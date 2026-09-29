"""The machine ledger, the deterministic status table, and the bounded human review.

Statuses are computed here from outcome files, never taken from a model (invariant 2).
The headline is about the paper's CENTRAL claims only; checks no central claim rests on are
reported apart as incidental, and whether the workflow finished is reported apart again.
Readings of one printed object that disagree (a changed indexing, an added assumption) are
recorded as conflicts with their deviations, never resolved here. The model-written summary
is published only if every status word it uses is earned by a check whose harness status
supports it; otherwise the table stands alone. There is no accept/reject line and no score.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from . import state
from .evidence import flat
from .execute import EVIDENCE
from .reconcile import FAILURE, SUPPORT

_SUPPORT_WORDS = ("verif(?!ier)", "reproduc", "confirm", "validat(?!ion)", "replicat", "corroborat")  # a validation set is data
_FAILURE_WORDS = ("refut", "disprov", "counterexampl", "contradict", "falsif", "failed to reproduc",
                  "failed reproduc")
_RANK = {"FATAL": 3, "MAJOR": 2, "MINOR": 1, "NOTE": 0}
_TESTED = SUPPORT + FAILURE + ("NO_VIOLATION_FOUND",)


def _claim_status(statuses: list[str], conflicted: bool = False) -> str:
    if conflicted:
        return "CONFLICTING"
    if any(s in FAILURE for s in statuses):
        return "VERIFIED_FAILURE"
    if any(s in SUPPORT for s in statuses):
        return "VERIFIED_SUPPORT"
    return "TESTED_NO_VIOLATION" if "NO_VIOLATION_FOUND" in statuses else "NOT_VERIFIED"


def _checks(root: Path, plan: dict) -> list[dict]:
    central = {k for cc in plan["central_claims"] for k in cc["checks"]}
    out = []
    for c in plan["checks"]:
        cdir = root / "checks" / c["id"]
        o = state.read_json(cdir / "outcome.json", {}) or {"status": "PENDING", "reason": "not finished"}
        run = state.read_json(cdir / "check.json", {})
        ev = EVIDENCE.get(c["kind"], "NONE")
        if c["kind"] == "CERTIFICATE" and c.get("step"):
            ev = "PROOF_AUDIT"
        out.append({"id": c["id"], "kind": c["kind"], "evidence": ev, "concerns": c["concerns"],
                    "central": c["id"] in central, "incidental_why": c.get("incidental_why", ""),
                    "claim": c["claim"], "target": c.get("target"), "statement": c.get("statement", ""),
                    "step": c.get("step", ""), "printed": c.get("printed", ""), "metric": run.get("metric", c.get("metric", "")),
                    "command": run.get("command", ""), "identity": run.get("identity"),
                    "script_sha256": run.get("script_sha256", ""), "deviations": run.get("deviations", []),
                    "runs": o.get("runs"), "values": o.get("values"), "literal": o.get("literal"),
                    "status": o.get("status"), "reason": o.get("reason", ""), "rule": o.get("rule", ""),
                    "environment": o.get("environment"), "authorization": o.get("authorization", ""),
                    "commit": o.get("commit", ""), "records": "execution.jsonl" if o.get("runs") else ""})
    return out


def _readings(c: dict) -> list[dict]:
    """Each reading of its printed object a check evaluated: its own (with its recorded
    deviations) and, when it also evaluated the text exactly as printed, that one."""
    res = "holds" if c["status"] in SUPPORT + ("NO_VIOLATION_FOUND",) else "fails" if c["status"] in FAILURE else ""
    if not res:
        return []
    out = [{"check": c["id"], "reading": "with recorded deviations" if c["deviations"] else "as printed",
            "result": res, "deviations": c["deviations"]}]
    if c["deviations"] and (lit := c.get("literal") or {}).get("n"):
        out.append({"check": c["id"], "reading": "as printed", "result": "fails" if lit["violated"] else "holds",
                    "deviations": [], "instances": f"{lit['violated']} of {lit['n']} instances fail or are undefined"})
    return out


def conflicts(checks: list[dict]) -> list[dict]:
    """Readings of the same printed object (a statement, a proof step, a target) that
    disagree, with what separates them. Explained: they differ by recorded deviations."""
    groups: dict[str, list] = {}
    for c in checks:
        key = flat(c["step"] or c["statement"]) if c["kind"] == "CERTIFICATE" else json.dumps(
            {k: v for k, v in (c.get("target") or {}).items() if k in ("quote", "row_quote", "column_quote", "value")},
            sort_keys=True)
        groups.setdefault(key, []).extend(_readings(c))
    out = []
    for key, rs in groups.items():
        held, failed = [r for r in rs if r["result"] == "holds"], [r for r in rs if r["result"] == "fails"]
        if held and failed:
            dk = lambda r: sorted((d["printed"], d["used"]) for d in r["deviations"])
            same = any(dk(a) == dk(b) for a in held for b in failed)
            first = next(c for c in checks if c["id"] == rs[0]["check"])
            out.append({"object": (first["step"] or first["statement"] or (first.get("target") or {}).get("quote")
                                   or first["printed"])[:300], "checks": sorted({r["check"] for r in rs}), "readings": rs,
                        "explained": not same,
                        "note": "two readings that make the same recorded choices disagree: unexplained, for a human"
                        if same else "the readings differ by the recorded deviations: which one the paper means is for "
                                     "a human to decide"})
    return out


def _central(plan: dict, checks: list[dict], conf: list[dict]) -> list[dict]:
    by_id, conflicted = {c["id"]: c for c in checks}, {k for x in conf for k in x["checks"]}
    return [{**cc, "statuses": {k: by_id[k]["status"] for k in cc["checks"] if k in by_id},
             "claim_status": _claim_status([by_id[k]["status"] for k in cc["checks"] if k in by_id],
                                           bool(conflicted & set(cc["checks"]))),
             "deviations": sum(len(by_id[k]["deviations"]) for k in cc["checks"] if k in by_id)}
            for cc in plan["central_claims"]]


def _headline(checks: list[dict], claims: list[dict]) -> str:
    """The paper-level status: about central claims only (incidental checks never lift it)."""
    if any(c["status"] == "PENDING" for c in checks):
        return "CHECKS_PENDING"
    st = [c["claim_status"] for c in claims]
    if "VERIFIED_FAILURE" in st:
        return "CENTRAL_CLAIM_FAILED"
    if "CONFLICTING" in st:
        return "CENTRAL_CLAIM_CONFLICTING"
    if st and all(s == "VERIFIED_SUPPORT" for s in st):
        return "CENTRAL_CLAIMS_SUPPORTED"
    return "SOME_CENTRAL_CLAIMS_SUPPORTED" if "VERIFIED_SUPPORT" in st else "NO_CENTRAL_CLAIM_VERIFIED"


def scientific_status(root: Path) -> str:
    """What the review CHECKED about the central claims — separate from workflow completion."""
    plan = state.read_json(root / "sealed" / "plan.json")
    if plan is None:
        return "NOT_ASSESSED"
    checks = _checks(root, plan)
    return _headline(checks, _central(plan, checks, conflicts(checks)))


def _workflow(checks: list[dict]) -> dict:
    by = lambda *s: [c["id"] for c in checks if c["status"] in s]
    return {"checks_planned": len(checks), "finished": len([c for c in checks if c["status"] != "PENDING"]),
            "reached_a_scientific_status": by(*_TESTED), "blocked": by("BLOCKED"),
            "inconclusive": by("INCONCLUSIVE"), "not_checkable": by("NOT_CHECKABLE"),
            "environment_recoveries": {c["id"]: c["environment"]["recovery"] for c in checks
                                       if (c.get("environment") or {}).get("recovery")}}


def ledger(x) -> dict:
    """Every report claim joined to its machine artifact; written to ledger.json."""
    plan = x.sealed("plan") or {"checks": [], "central_claims": [], "dropped": []}
    checks = _checks(x.root, plan)
    conf = conflicts(checks)
    central = _central(plan, checks, conf)
    lens_drops = [d for lens in ("overclaim", "protocol", "confound", "contradiction")
                  for d in (x.sealed(f"lens:{lens}") or {}).get("dropped", [])]
    out = {"paper": {k: x.meta.get(k, "") for k in ("title", "sha256", "arxiv_id", "arxiv_version", "source")},
           "source": {k: x.src.get(k, "") for k in ("url", "commit", "discovered_by", "evidence", "status")},
           "repo_is_authors": plan.get("repo_is_authors"), "repo_note": plan.get("repo_note", ""),
           "concerns": sorted(x.concerns(), key=lambda c: (-_RANK[c["severity"]], c["id"])),
           "checks": checks, "central_claims": central, "incidental_checks": [c["id"] for c in checks if not c["central"]],
           "conflicts": conf, "workflow": _workflow(checks),
           "dropped": {"concerns_unresolved_quotes": lens_drops, "checks": plan.get("dropped", [])},
           "scientific_status": _headline(checks, central)}
    state.write_json(x.root / "ledger.json", out)
    return out


def _cell(s, n: int = 90) -> str:
    s = re.sub(r"\s+", " ", str(s or "")).replace("|", "/")
    return s if len(s) <= n else s[:n - 1] + "…"


def _check_rows(checks: list[dict], led: dict) -> list[str]:
    rows = ["| Check | Kind (evidence) | Bears on | Printed target | Result | Status | Deviations | Rule or blocker |",
            "|---|---|---|---|---|---|---|---|"]
    for c in checks:
        t = c.get("target") or {}
        target = t.get("relation") or c["printed"] or _cell(c["step"] or c["statement"], 60)
        got = "" if not c.get("values") else (("margin " if t.get("relation") else "")
                                              + f"{sum(c['values']) / len(c['values']):.6g} (n={len(c['values'])})")
        rows.append(f"| {c['id']} | {c['kind']} ({c['evidence']}) | {_cell(c['claim'], 70)} | {_cell(target, 40)} | "
                    f"{got} | **{c['status']}** | {len(c['deviations']) or ''} | "
                    f"{_said(_cell(c['rule'] or c['reason'], 160), led)} |")
    return rows


def table(led: dict) -> str:
    """The deterministic status block: central claims, their checks, incidental checks, and
    workflow completion, each apart."""
    rows = ["| Central claim | Checks | Claim status | Deviations | Why unchecked |", "|---|---|---|---|---|"]
    rows += [f"| {_cell(cc['quote'], 110)} (p{cc['page']}) | "
             f"{', '.join(f'{k} {s}' for k, s in cc['statuses'].items()) or '—'} | **{cc['claim_status']}** | "
             f"{cc.get('deviations') or ''} | {_said(_cell(cc['why_unchecked'], 140), led)} |" for cc in led["central_claims"]]
    if not led["central_claims"]:
        rows.append("| — the planner listed no central claim | | | | |")
    central = [c for c in led["checks"] if c["central"]]
    incidental = [c for c in led["checks"] if not c["central"]]
    rows += ["", "Checks bearing on central claims:", ""] + (_check_rows(central, led) if central else ["_none_"])
    rows += ["", "Incidental checks (no central claim rests on them; they never change the status above):", ""] + (
        _check_rows(incidental, led) + [f"- {c['id']}: {_said(_cell(c['incidental_why'], 200), led)}"
                                        for c in incidental if c["incidental_why"]] if incidental else ["_none_"])
    w = led["workflow"]
    rows += ["", f"Workflow completion: {w['finished']} of {w['checks_planned']} planned checks finished; "
                 f"{len(w['reached_a_scientific_status'])} reached a scientific status "
                 f"({', '.join(w['reached_a_scientific_status']) or 'none'}); blocked: {', '.join(w['blocked']) or 'none'}; "
                 f"inconclusive: {', '.join(w['inconclusive']) or 'none'}; not checkable: "
                 f"{', '.join(w['not_checkable']) or 'none'}."]
    return "\n".join(rows)


def unearned(text: str, led: dict) -> list[str]:
    """Sentences using a status word (any inflection, through markdown or look-alike
    characters) that no check cited in the same sentence earns. Negated use ("was not
    reproduced", "unverified") is fine."""
    status = {c["id"]: c["status"] for c in led["checks"]}
    # Harness vocabulary (RESOLVED_VERIFIED, AUTHOR_CODE_REPRODUCTION...) and the ledger's own
    # concern ids (contradiction-02) name things; they are not claims.
    text = flat_text(text)
    for cid in sorted((c["id"] for c in led.get("concerns", [])), key=len, reverse=True):
        text = text.replace(cid, " ")
    norm = re.sub(r"[*_`~]", "", re.sub(r"\b[A-Z]+(?:_[A-Z]+)+\b", " ", text))
    bad = []
    for sentence in re.split(r"(?<=[.!?])\s+|\n+", norm):
        if sentence.rstrip().endswith("?"):
            continue                    # a question asserts no status
        low, ids = sentence.lower(), set(re.findall(r"\bC\d+\b", sentence))
        for stems, earned in ((_SUPPORT_WORDS, SUPPORT), (_FAILURE_WORDS, FAILURE)):
            for m in re.finditer(r"\b(?:un)?(?:" + "|".join(stems) + r")\w*", low):
                negated = m.group().startswith("un") or re.search(
                    r"\b(?:not|no|never|cannot|without|nothing)\b|n't", low[max(0, m.start() - 30):m.start()])
                if not negated and not any(status.get(i) in earned for i in ids):
                    bad.append(sentence.strip()[:200])
                    break
    return bad


def flat_text(s: str) -> str:
    import unicodedata
    return unicodedata.normalize("NFKC", s or "")


def _said(s: str, led: dict) -> str:
    """Model-written text as rendered in the review: withheld if it claims a status."""
    return "_(withheld: status language no check earns)_" if unearned(s, led) else s


def render(x, led: dict, rep: dict | None) -> str:
    p, s = led["paper"], led["source"]
    summary = (rep or {}).get("summary_md", "")
    bad = unearned(summary, led) + [f"quote not in the paper: {q[:80]!r}"
                                    for q in re.findall(r'"([^"\n]{20,})"', summary) if x.paper.find(q)[0] is None]
    fails = [c for c in led["checks"] if c["status"] in FAILURE]
    lines = [f"# Review: {p['title']}", "",
             f"PDF sha256 `{p['sha256'][:16]}`" + (f", arXiv {p['arxiv_id']}{p['arxiv_version']}" if p["arxiv_id"] else "")
             + (f"; code {s['url']} @ `{s['commit'][:12]}` (attributed by {s['discovered_by']})" if s["url"] else
                "; no author repository attributed"),
             "", "_A first-pass review aid for a human referee. It makes no accept/reject recommendation; "
                 "severities and interpretations are model judgments, statuses are computed by the harness._",
             "", f"## Central claims — status computed by the harness: {led['scientific_status']}", "", table(led), ""]
    if fails:
        lines += ["## Established failures", ""] + [
            f"- **{c['id']}** {c['status']} ({c['evidence']}{', central' if c['central'] else ', incidental'}): "
            f"{c['reason']} — see `ledger.json`, `{c['records'] or 'outcome'}`" for c in fails] + [""]
    if led["conflicts"]:
        lines += ["## Conflicting readings of one printed object (recorded, not resolved)", ""]
        for cf in led["conflicts"]:
            lines.append(f"- {_cell(cf['object'], 200)} — {cf['note']}")
            lines += [f"  - {r['check']} {r['reading']}: **{r['result']}**" + (f" ({r['instances']})" if r.get("instances") else "")
                      + "".join(f"; printed \"{_cell(d['printed'], 80)}\" -> used: {_cell(d['used'], 120)}"
                                for d in r["deviations"]) for r in cf["readings"]]
        lines.append("")
    devs = [(c["id"], d) for c in led["checks"] for d in c["deviations"]]
    recs = led["workflow"]["environment_recoveries"]
    if devs or recs:
        lines += ["## Recorded deviations and environment recoveries", ""] + [
            f"- {cid}: " + (f"printed \"{_cell(d['printed'], 120)}\" (p{d['page']})" if d["printed"] else "the paper is silent")
            + f" -> used: {_cell(d['used'], 200)}. Why: {_cell(d['why'], 200)}" for cid, d in devs] + [
            f"- {cid} environment: {r['action']}" for cid, rs in recs.items() for r in rs] + [""]
    lines += ["## Summary (model-written from the ledger)", "",
              summary if summary and not bad else
              f"_Withheld: the summary used status language no check earns: {bad[:2]}_" if bad else "_No summary._", ""]
    lines += ["## Concerns (after the critic)", ""]
    for c in [c for c in led["concerns"] if not c.get("withdrawn")][:15]:   # ponytail: 15 shown, all in the ledger
        q = "; ".join(f"\"{_cell(e['quote'], 160)}\" (p{e['page']})" for e in c["evidence"][:2])
        lines.append(f"- **{c['id']} {c['severity']}/{c['confidence']}** {_said(c['title'], led)} — "
                     f"{_said(_cell(c['statement'], 300), led)} Evidence: {q}")
    withdrawn = [c for c in led["concerns"] if c.get("withdrawn")]
    if withdrawn:
        lines += ["", f"_{len(withdrawn)} concern(s) withdrawn by the critic, with reasons, are in `ledger.json`._"]
    dropped = led["dropped"]
    lines += ["", "## Not checked", ""] + [
        f"- {_cell(cc['quote'], 140)}: {_said(cc['why_unchecked'], led) or 'no check reached a conclusion'}"
        for cc in led["central_claims"] if cc["claim_status"] == "NOT_VERIFIED"] + [
        f"- planned check {d['check']} dropped: {_cell(d['why'], 200)}" for d in dropped["checks"]] + [
        f"- {len(dropped['concerns_unresolved_quotes'])} concern(s) dropped because a quote did not resolve (ledger)."]
    text = "\n".join(lines) + "\n"
    (x.root / "review.md").write_text(text, encoding="utf-8")
    return text
