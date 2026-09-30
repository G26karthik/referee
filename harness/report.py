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
from .reconcile import FAILURE, PARTIAL, SUPPORT

_SUPPORT_WORDS = ("verif(?!ier)", "reproduc", "confirm", "validat(?!ion)", "replicat", "corroborat")  # a validation set is data
_FAILURE_WORDS = ("refut", "disprov", "counterexampl", "contradict", "falsif", "failed to reproduc",
                  "failed reproduc")
# Protocol nouns ("replicate count", "3 seeded replicates") name units of a run, not a replication.
_NOUNS = re.compile(r"\b(?:\d+|seeded|independent|per|of)\s+replicates?\b|\breplicates?\s+(?:count|number)s?\b", re.I)
_STAGES_SHOWN = 40   # ponytail: stages listed per check in review.md; every stage is in ledger.json
_RANK = {"FATAL": 3, "MAJOR": 2, "MINOR": 1, "NOTE": 0}
_TESTED = SUPPORT + FAILURE + ("NO_VIOLATION_FOUND",)


def merged(plan: dict | None, follow: dict | None) -> dict | None:
    """The first plan with its follow-up round: the follow-up's checks appended, its links and
    reasons joined onto the same central claims (matched by quote)."""
    if not plan or not follow:
        return plan
    claims = [dict(cc) for cc in plan["central_claims"]]
    for f in follow.get("central_claims", []):
        cc = next((c for c in claims if flat(c["quote"]) == flat(f["quote"])), None)
        if cc is None:
            claims.append(f)
            continue
        cc["checks"] = cc["checks"] + [k for k in f["checks"] if k not in cc["checks"]]
        cc["why_unchecked"] = f.get("why_unchecked") or cc.get("why_unchecked", "")
        cc["omitted"] = (cc.get("omitted") or []) + (f.get("omitted") or [])
    return {**plan, "checks": plan["checks"] + follow["checks"], "central_claims": claims,
            "dropped": plan.get("dropped", []) + follow.get("dropped", [])}


def _changed(c: dict) -> bool:
    return any(d.get("changes_claim") for d in c.get("deviations") or [])


def _claim_status(cs: list[dict], conflicted: bool = False) -> str:
    """What the checks of one central claim FOUND. A failed step of a printed proof is a gap in
    the proof, never a refutation of the statement; a premise no tested instance met, or a
    result obtained only after changing what the claim says (a premise, a definition, the
    method's tuning or training data...), is neither support nor failure of the printed claim;
    a check whose protocol did not complete is partial evidence, whatever its completed part says."""
    if not cs:
        return "NOT_CHECKED"
    if conflicted:
        return "CHECKS_DISAGREE"
    # A certificate separates its readings itself (COUNTEREXAMPLE_FOUND is always about the text as
    # printed); any other check's result under a claim-changing deviation is about the changed claim.
    moved = lambda c: _changed(c) and c["kind"] != "CERTIFICATE"
    fails = [c for c in cs if c["status"] in FAILURE and not moved(c)]
    if any(c["evidence"] != "PROOF_AUDIT" for c in fails):
        return "FAILURE_FOUND"
    if fails:
        return "PROOF_GAP_FOUND"
    if any(c["status"] in SUPPORT and not moved(c) for c in cs):
        return "SUPPORT_FOUND"
    if any(c["status"] == "PREMISE_NOT_MET" for c in cs):
        return "PREMISE_NOT_MET"
    if any(c["status"] == "VIOLATION_UNDER_CHANGED_READING" or (c["status"] in SUPPORT + FAILURE and moved(c))
           or (c["status"] == "NO_VIOLATION_FOUND" and _changed(c)) for c in cs):
        return "READING_CHANGED"
    if any(c["status"] == "NO_VIOLATION_FOUND" for c in cs):
        return "NO_VIOLATION_FOUND"
    if any(c["status"] == PARTIAL for c in cs):
        return "PARTIAL_EVIDENCE"
    return "PENDING" if any(c["status"] == "PENDING" for c in cs) else "NOTHING_DECIDED"


def _state(root: Path, cid: str, o: dict) -> str:
    """Where a check is: not started, running, partially completed, completed, failed, resource-limited."""
    if not o:
        return "RUNNING" if (root / "checks" / cid / "exec.json").exists() else "NOT_STARTED"
    s = o.get("status")
    if s == PARTIAL:
        return "PARTIALLY_COMPLETED"
    if s == "BLOCKED":
        return "RESOURCE_LIMITED" if str(o.get("reason", "")).startswith("RESOURCE BLOCKER") else "REFUSED_BY_GATE"
    if s == "NOT_CHECKABLE":
        return "NOT_RUN"
    if s == "INCONCLUSIVE" and not o.get("values"):
        return "FAILED"
    if s == "INCONCLUSIVE" and (o.get("runs") or 0) < ((o.get("protocol") or {}).get("runs") or 0):
        return "PARTIALLY_COMPLETED"             # ended (by the host, the operator) before its planned runs
    return "COMPLETED"


def _done(c: dict) -> str:
    """What one check did, in a few words, and its status."""
    n = len(c.get("values") or [])
    adm = f", {c['admissible']} admissible" if c.get("admissible") is not None else ""
    lit = "".join(f", as printed {v} {k}" for k, v in (c.get("literal") or {}).items() if v)
    what = {"CERTIFICATE": f"{n} exact instance(s){adm}{lit}", "ARITHMETIC": "the paper's own operands"}.get(
        c["kind"], f"{c.get('runs') or 0} run(s), {n} result(s)")
    st = c.get("stages") or {}
    stages = "".join(f"; {s}: {p['status']}" + (f" n={p['n']}" if p.get("n") else "")
                     for s, p in st.items()) if len(st) <= 6 else f"; {len(st)} stages: {_counts(st)} (see Stages)"
    extra = (f" (on what completed: {c['status_on_completed']})" if c.get("status_on_completed") else "")
    return (f"{c['id']} {c['kind']}{' proof step' if c['evidence'] == 'PROOF_AUDIT' else ''} on {what}{stages}"
            f"{' (claim reading changed)' if _changed(c) else ''}: {c['status']}{extra}")


def _counts(stages: dict) -> str:
    n: dict = {}
    for p in stages.values():
        n[p["status"]] = n.get(p["status"], 0) + 1
    return ", ".join(f"{v} {k}" for k, v in sorted(n.items(), key=lambda kv: (-kv[1], kv[0])))


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
        # Superseded outcomes (an earlier run of this check that a later one replaced) stay visible.
        history = [{"file": f.name, "status": (state.read_json(f) or {}).get("status"),
                    "reason": str((state.read_json(f) or {}).get("reason", ""))[:300]}
                   for f in sorted(cdir.glob("outcome.*.json"))]
        out.append({"id": c["id"], "kind": c["kind"], "evidence": ev, "concerns": c["concerns"],
                    "central": c["id"] in central, "incidental_why": c.get("incidental_why", ""),
                    "claim": c["claim"], "target": c.get("target"), "statement": c.get("statement", ""),
                    "step": c.get("step", ""), "printed": c.get("printed", ""), "metric": run.get("metric", c.get("metric", "")),
                    "covers": c.get("covers", []), "acquire": c.get("acquire", []),
                    "command": run.get("command", ""), "identity": run.get("identity"),
                    "script_sha256": run.get("script_sha256", ""), "deviations": run.get("deviations", []),
                    "premise_argument": run.get("premise_argument", ""),
                    "state": _state(root, c["id"], state.read_json(cdir / "outcome.json", {}) or {}),
                    # an outcome decided from checkpointed seeds before they counted as runs says so in its protocol
                    "runs": o.get("runs") or (o.get("protocol") or {}).get("seeds_reused_from_checkpoints"),
                    "values": o.get("values"), "literal": o.get("literal"),
                    "admissible": o.get("admissible"), "protocol": o.get("protocol"),
                    "image_check": o.get("image_check"), "pilot_values": o.get("pilot_values"),
                    "status": o.get("status"), "reason": o.get("reason", ""), "reason_by": o.get("reason_by", "harness"),
                    "rule": o.get("rule") or next((p["rule"] for p in (o.get("stages") or {}).values() if p.get("rule")), ""),
                    "stages": o.get("stages"), "status_on_completed": o.get("status_on_completed"),
                    "completed_stages": o.get("completed_stages"),
                    "failed_seeds": o.get("failed_seeds"), "data_identity": o.get("data_identity"), "data": o.get("data"),
                    "resource": o.get("resource"), "stage_times": o.get("stage_times"), "peak_mb": o.get("peak_mb"),
                    "environment": o.get("environment"), "authorization": o.get("authorization", ""),
                    "commit": o.get("commit", ""), "history": history,
                    "records": "execution.jsonl" if o.get("runs") or (o.get("protocol") or {}).get(
                        "seeds_reused_from_checkpoints") else ""})
    return out


def _readings(c: dict) -> list[dict]:
    """The reading of its printed object a check decided (the text as printed, or as changed by
    its recorded claim-changing deviations). A check's own as-printed evaluation is part of its
    status (COUNTEREXAMPLE_FOUND, READING_CHANGED with its literal counts), not a conflict."""
    res = "holds" if c["status"] in SUPPORT + ("NO_VIOLATION_FOUND",) else "fails" if c["status"] in FAILURE else ""
    devs = [d for d in c["deviations"] if d.get("changes_claim")]
    return [{"check": c["id"], "reading": "with claim-changing deviations" if devs else "as printed",
             "result": res, "deviations": devs}] if res else []


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
             "claim_status": _claim_status([by_id[k] for k in cc["checks"] if k in by_id],
                                           bool(conflicted & set(cc["checks"]))),
             "deviations": sum(len(by_id[k]["deviations"]) for k in cc["checks"] if k in by_id)}
            for cc in plan["central_claims"]]


def _headline(checks: list[dict], claims: list[dict]) -> str:
    """The paper-level status: about central claims only (incidental checks never lift it)."""
    if any(c["status"] == "PENDING" for c in checks):
        return "CHECKS_PENDING"
    st = [c["claim_status"] for c in claims]
    for found, head in (("FAILURE_FOUND", "CENTRAL_FAILURE_FOUND"), ("CHECKS_DISAGREE", "CENTRAL_CHECKS_DISAGREE"),
                        ("PROOF_GAP_FOUND", "CENTRAL_PROOF_GAP_FOUND")):
        if found in st:
            return head
    if st and all(s == "SUPPORT_FOUND" for s in st):
        return "CENTRAL_SUPPORT_FOUND"
    if "SUPPORT_FOUND" in st:
        return "SOME_CENTRAL_SUPPORT_FOUND"
    for found in ("PREMISE_NOT_MET", "READING_CHANGED", "NO_VIOLATION_FOUND", "PARTIAL_EVIDENCE"):
        if found in st:
            return f"CENTRAL_{found}"
    return "NO_CENTRAL_FINDING"


def scientific_status(root: Path) -> str:
    """What the review CHECKED about the central claims — separate from workflow completion."""
    plan = merged(state.read_json(root / "sealed" / "plan.json"), state.read_json(root / "sealed" / "plan__2.json"))
    if plan is None:
        return "NOT_ASSESSED"
    checks = _checks(root, plan)
    return _headline(checks, _central(plan, checks, conflicts(checks)))


def _workflow(checks: list[dict]) -> dict:
    by = lambda *s: [c["id"] for c in checks if c["status"] in s]
    return {"checks_planned": len(checks), "finished": len([c for c in checks if c["status"] != "PENDING"]),
            "reached_a_scientific_status": by(*_TESTED), "blocked": by("BLOCKED"), "partial": by(PARTIAL),
            "inconclusive": by("INCONCLUSIVE"), "not_checkable": by("NOT_CHECKABLE"),
            "states": {c["id"]: c.get("state") for c in checks},
            "environment_recoveries": {c["id"]: c["environment"]["recovery"] for c in checks
                                       if (c.get("environment") or {}).get("recovery")}}


def ledger(x) -> dict:
    """Every report claim joined to its machine artifact; written to ledger.json."""
    plan = x.plan() or {"checks": [], "central_claims": [], "dropped": []}
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
    rows = ["| Check | Kind (evidence) | Bears on | Printed target | Result | Status (state) | Deviations | Rule or blocker |",
            "|---|---|---|---|---|---|---|---|"]
    for c in checks:
        t = c.get("target") or {}
        target = t.get("relation") or c["printed"] or _cell(c["step"] or c["statement"], 60)
        got = "" if not c.get("values") else (("margin " if t.get("relation") else "")
                                              + f"{sum(c['values']) / len(c['values']):.6g} (n={len(c['values'])})")
        if c.get("stages"):
            got = "; ".join(f"{s}: " + (f"{p.get('margin', p.get('reproduced', ''))} n={p['n']}" if p.get("n") else
                                         p["status"]) for s, p in c["stages"].items()) \
                if len(c["stages"]) <= 6 else f"{len(c['stages'])} stages: {_counts(c['stages'])}"
        elif c.get("completed_stages"):              # per stage, never pooled across stages; decides nothing
            cs = c["completed_stages"]
            got = ("margin " if t.get("relation") else "") + ("; ".join(
                f"{s}: {p['mean']:.6g} n={p['n']}" for s, p in cs.items()) if len(cs) <= 6 else f"{len(cs)} stages (see Stages)")
        # A harness rule states a fact and is shown as is; a reason may carry a script's or a model's
        # own words (a stderr tail, a refusal), so it is screened for status words.
        rule = c["status"] in SUPPORT + FAILURE and c["rule"]
        why = _cell(c["rule"] if rule else c["reason"] or c["rule"], 160)
        rows.append(f"| {c['id']} | {c['kind']} ({c['evidence']}) | {_cell(c['claim'], 70)} | {_cell(target, 40)} | "
                    f"{_cell(got, 80)} | **{c['status']}** ({c.get('state', '')}) | {len(c['deviations']) or ''} | "
                    f"{why if rule else _said(why, led)} |")
    return rows


def table(led: dict) -> str:
    """The deterministic status block: central claims, their checks, incidental checks, and
    workflow completion, each apart."""
    by_id = {c["id"]: c for c in led["checks"]}
    rows = ["| Central claim | What was done | Found | Deviations | Why unchecked |", "|---|---|---|---|---|"]
    rows += [f"| {_cell(cc['quote'], 110)} (p{cc['page']}) | "
             f"{'; '.join(_done(by_id[k]) for k in cc['statuses']) or '—'} | **{cc['claim_status']}** | "
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
                 f"({', '.join(w['reached_a_scientific_status']) or 'none'}); partial: {', '.join(w['partial']) or 'none'}; "
                 f"blocked: {', '.join(w['blocked']) or 'none'}; "
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
    norm = re.sub(r"[*_`~]", "", _NOUNS.sub(" ", re.sub(r"\b[A-Z]+(?:_[A-Z]+)+\b", " ", text)))
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


def unquoted(text: str, paper) -> list[str]:
    """Quotes (20+ characters) in model prose that occur nowhere in the paper. Quote marks pair in
    order, so a short quote's closing mark never opens the text up to the next quote; a quote copied
    with the paper's line break escaped ("incompara-\\nble") is the same quote."""
    return [q for q in re.findall(r'"([^"\n]*)"', text or "")
            if len(q) >= 20 and not paper.occurs(q.replace("\\n", "\n"))]


def flat_text(s: str) -> str:
    import unicodedata
    return unicodedata.normalize("NFKC", s or "")


def _said(s: str, led: dict) -> str:
    """Model-written text as rendered in the review: withheld if it claims a status."""
    return "_(withheld: status language no check earns)_" if unearned(s, led) else s


def render(x, led: dict, rep: dict | None) -> str:
    p, s = led["paper"], led["source"]
    summary = (rep or {}).get("summary_md", "")
    # A quote copied with the paper's line break escaped ("incompara-\nble") is the same quote.
    bad = unearned(summary, led) + [f"quote not in the paper: {q[:80]!r}" for q in unquoted(summary, x.paper)]
    fails = [c for c in led["checks"] if c["status"] in FAILURE]
    lines = [f"# Review: {p['title']}", "",
             f"PDF sha256 `{p['sha256'][:16]}`" + (f", arXiv {p['arxiv_id']}{p['arxiv_version']}" if p["arxiv_id"] else "")
             + (f"; code {s['url']} @ `{s['commit'][:12]}` (attributed by {s['discovered_by']})" if s["url"] else
                "; no author repository attributed"),
             "", "_A first-pass review aid for a human referee. It makes no accept/reject recommendation; "
                 "severities and interpretations are model judgments, statuses are computed by the harness._",
             "", f"## Central claims — what the harness found: {led['scientific_status']}", "", table(led), ""]
    if fails:
        lines += ["## Failures found", ""] + [
            f"- **{c['id']}** {c['status']} ({c['evidence']}{', central' if c['central'] else ', incidental'}): "
            + ("a step of the printed proof fails; the statement itself is not refuted. " if c["evidence"] == "PROOF_AUDIT"
               else "") + f"{c['reason']} — see `ledger.json`, `{c['records'] or 'outcome'}`" for c in fails] + [""]
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
            f"- {cid}{' **(changes the claim)**' if d.get('changes_claim') else ''}: "
            + (f"printed \"{_cell(d['printed'], 120)}\" (p{d['page']})" if d["printed"] else "the paper is silent")
            + f" -> used: {_cell(d['used'], 200)}. Why: {_cell(d['why'], 200)}" for cid, d in devs] + [
            f"- {cid} environment: {r['action']}" for cid, rs in recs.items() for r in rs] + [""]
    staged = [c for c in led["checks"] if c.get("stages") or c.get("failed_seeds") or c.get("completed_stages")]
    if staged:
        lines += ["## Stages and partial results (completed measurements are kept when a later stage fails)", ""]
        for c in staged:
            sts = c.get("stages") or {}
            lines.append(f"- **{c['id']}** {c['status']} ({c.get('state')})"
                         + (f"; on what completed: {c['status_on_completed']}" if c.get("status_on_completed") else "")
                         + (f"; {len(sts)} stages: {_counts(sts)}" if len(sts) > 6 else ""))
            lines += [f"  - stage {st}: {pr['status']}" + (f", n={pr['n']}" if pr.get("n") else "")
                      + (f", margin {pr['margin']}, t*SE {pr['band']}" if "margin" in pr and "band" in pr else "")
                      + (f", mean {pr['reproduced']}" if "reproduced" in pr else "") + f" — {_said(_cell(pr['reason'], 200), led)}"
                      for st, pr in list(sts.items())[:_STAGES_SHOWN]]
            if len(sts) > _STAGES_SHOWN:
                lines.append(f"  - … {len(sts) - _STAGES_SHOWN} more stages, each with its status and reason in "
                             f"`ledger.json` (checks[{c['id']}].stages)")
            lines += [f"  - stage {st}: n={pr['n']}, mean {pr['mean']} — completed before the check ended; decides nothing"
                      for st, pr in list((c.get("completed_stages") or {}).items())[:_STAGES_SHOWN]]
            lines += [f"  - seed {k} did not complete: {_said(_cell(v, 220), led)}"
                      for k, v in sorted((c.get("failed_seeds") or {}).items())]
        lines.append("")
    got = [c for c in led["checks"] if c.get("acquire") or c.get("data_identity")]
    if got:
        lines += ["## Data acquired and data identity (provenance, as recorded)", ""]
        for c in got:
            d = state.read_json(x.root / "checks" / c["id"] / "data.json") or {}
            for src in d.get("sources") or c.get("acquire") or []:
                lines.append(f"- **{c['id']}** source `{src.get('source')}`"
                             + (f" (HTTP {src['http_status']})" if src.get("http_status") else "")
                             + (f" revision `{src['revision'][:12]}`" if src.get("revision") else "")
                             + (f" — ERROR: {_cell(src['error'], 200)}" if src.get("error") else "")
                             + "".join(f"; followed {f.get('url')}" + (f" ERROR {_cell(f['error'], 80)}" if f.get("error")
                                                                        else "") for f in (src.get("followed") or [])[:5]))
            if d.get("n_files") is not None:
                lines.append(f"  - {d.get('n_files')} files, {d.get('bytes')} bytes, each with sha256 in "
                             f"`checks/{c['id']}/data.json` (fetched {d.get('fetched_at', '?')})")
            for name, ident in (c.get("data_identity") or {}).items():
                lines.append(f"  - dataset {name}: {_said(_cell(json.dumps(ident, ensure_ascii=False), 300), led)}")
        lines.append("")
    hist = [(c["id"], h) for c in led["checks"] for h in c.get("history") or []]
    if hist:
        lines += ["## Superseded results (withdrawn by a later run of the same check)", ""] + [
            f"- {cid} `{h['file']}`: {h['status']} — {_cell(h['reason'], 200)}" for cid, h in hist] + [""]
    prot = [(c["id"], c["protocol"]) for c in led["checks"] if c.get("protocol")]
    if prot:
        lines += ["## Protocol choices (what the paper stated, what REFEREE supplied)", ""]
        for cid, pr in prot:
            rule = pr["decision_rule"] or next(c["rule"] for c in led["checks"] if c["id"] == cid)
            lines.append(f"- **{cid}**: runs {pr['runs']} ({pr['runs_from']}); seeds {pr['seeds']}; rule: "
                         f"{_cell(rule, 220)}" + (f"; relation {pr['relation']}" if pr.get("relation") else "")
                         + (f"; pilot {pr['pilot_seconds']:.0f}s" if pr.get("pilot_seconds") else ""))
            lines += [f"  - REFEREE supplied: {_cell(u, 220)}" for u in pr.get("supplied_by_referee", [])
                      if u not in pr.get("claim_changes", [])]      # a claim-changing choice is listed once, below
            lines += [f"  - changes the claim: {_cell(u, 220)}" for u in pr.get("claim_changes", [])]
        lines.append("")
    lines += ["## Summary (model-written from the ledger)", "",
              summary if summary and not bad else
              f"_Withheld: the summary used status language no check earns: {bad[:2]}_" if bad else "_No summary._", ""]
    args = [c for c in led["checks"] if c.get("premise_argument")]
    if args:
        lines += ["## Arguments the script author gave (verifier-approved reasoning, not an executed result)", ""] + [
            f"- **{c['id']}**: {_said(_cell(c['premise_argument'], 600), led)}" for c in args] + [""]
    lines += ["## Concerns (after the critic)", ""]
    for c in [c for c in led["concerns"] if not c.get("withdrawn")][:15]:   # ponytail: 15 shown, all in the ledger
        q = "; ".join(f"\"{_cell(e['quote'], 160)}\" (p{e['page']})" for e in c["evidence"][:2])
        linked = ", ".join(f"{k['id']} {k['status']}" for k in led["checks"] if c["id"] in k["concerns"])
        lines.append(f"- **{c['id']} {c['severity']}/{c['confidence']}** {_said(c['title'], led)} — "
                     f"{_said(_cell(c['statement'], 300), led)} Evidence: {q}"
                     + (f" _Page-image check: {_cell(c['image_check'], 240)}._" if c.get("image_check") else "")
                     + (f" _Checked by: {linked} (a concern stands only as far as its check established it)._"
                        if linked else ""))
    withdrawn = [c for c in led["concerns"] if c.get("withdrawn")]
    if withdrawn:
        lines += ["", f"_{len(withdrawn)} concern(s) withdrawn by the critic, with reasons, are in `ledger.json`._"]
    dropped = led["dropped"]
    lines += ["", "## Not checked or not decided", ""] + [
        f"- {_cell(cc['quote'], 140)} — {cc['claim_status']}: {_said(cc['why_unchecked'], led) or 'no check reached a conclusion'}"
        for cc in led["central_claims"] if cc["claim_status"] in ("NOT_CHECKED", "NOTHING_DECIDED", "PENDING",
                                                                  "PARTIAL_EVIDENCE")] + [
        f"- {_cell(cc['quote'], 80)}: scope item **{o['item']}** omitted — {_said(_cell(o['why'], 200), led)}"
        for cc in led["central_claims"] for o in cc.get("omitted") or []] + [
        f"- planned check {d['check']} dropped: {_cell(d['why'], 200)}" for d in dropped["checks"]] + [
        f"- {len(dropped['concerns_unresolved_quotes'])} concern(s) dropped because a quote did not resolve (ledger)."]
    text = "\n".join(lines) + "\n"
    (x.root / "review.md").write_text(text, encoding="utf-8")
    return text
