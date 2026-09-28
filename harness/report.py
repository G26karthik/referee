"""The machine ledger, the deterministic status table, and the bounded human review.

Statuses are computed here from outcome files, never taken from a model (invariant 2).
The model-written summary is published only if every status word it uses is earned by a
check whose harness status supports it; otherwise the table stands alone (invariant 18).
There is no accept/reject line and no score (invariant 19).
"""
from __future__ import annotations

import re
from pathlib import Path

from . import state
from .execute import EVIDENCE
from .reconcile import FAILURE, SUPPORT

_SUPPORT_WORDS = ("verif(?!ier)", "reproduc", "confirm", "validat", "replicat", "corroborat")
_FAILURE_WORDS = ("refut", "disprov", "counterexampl", "contradict", "falsif", "failed to reproduc",
                  "failed reproduc")
_RANK = {"FATAL": 3, "MAJOR": 2, "MINOR": 1, "NOTE": 0}


def _claim_status(statuses: list[str]) -> str:
    if any(s in FAILURE for s in statuses):
        return "VERIFIED_FAILURE"
    return "VERIFIED_SUPPORT" if any(s in SUPPORT for s in statuses) else "NOT_VERIFIED"


def scientific_status(root: Path) -> str:
    """What the review CHECKED, paper-level — separate from whether the workflow finished."""
    statuses = [(state.read_json(p) or {}).get("status", "") for p in (root / "checks").glob("*/outcome.json")]
    if any(s in FAILURE for s in statuses):
        return "ESTABLISHED_FAILURE"
    if any(s in SUPPORT for s in statuses):
        return "SUPPORTED_BY_CHECK"
    planned = (state.read_json(root / "sealed" / "plan.json") or {}).get("checks")
    if planned and len(statuses) < len(planned):
        return "CHECKS_PENDING"
    if statuses:
        return "NO_CONCLUSIVE_CHECK"
    return "NO_CHECK_PLANNED" if planned is not None else "NOT_ASSESSED"


def ledger(x) -> dict:
    """Every report claim joined to its machine artifact; written to ledger.json."""
    plan = x.sealed("plan") or {"checks": [], "central_claims": [], "dropped": []}
    checks = []
    for c in plan["checks"]:
        cdir = x.root / "checks" / c["id"]
        o = state.read_json(cdir / "outcome.json", {}) or {"status": "PENDING", "reason": "not finished"}
        run = state.read_json(cdir / "check.json", {})
        ev = EVIDENCE.get(c["kind"], "NONE")
        if c["kind"] == "CERTIFICATE" and c.get("step"):
            ev = "PROOF_AUDIT"
        checks.append({"id": c["id"], "kind": c["kind"], "evidence": ev, "concerns": c["concerns"],
                       "claim": c["claim"], "target": c.get("target"), "statement": c.get("statement", ""),
                       "step": c.get("step", ""), "printed": c.get("printed", ""),
                       "command": run.get("command", ""), "identity": run.get("identity"),
                       "script_sha256": run.get("script_sha256", ""), "runs": o.get("runs"),
                       "values": o.get("values"), "status": o.get("status"), "reason": o.get("reason", ""),
                       "rule": o.get("rule", ""), "authorization": o.get("authorization", ""),
                       "commit": o.get("commit", ""), "records": "execution.jsonl" if o.get("runs") else ""})
    by_id = {c["id"]: c for c in checks}
    central = [{**cc, "statuses": {k: by_id[k]["status"] for k in cc["checks"] if k in by_id},
                "claim_status": _claim_status([by_id[k]["status"] for k in cc["checks"] if k in by_id])}
               for cc in plan["central_claims"]]
    lens_drops = [d for lens in ("overclaim", "protocol", "confound", "contradiction")
                  for d in (x.sealed(f"lens:{lens}") or {}).get("dropped", [])]
    out = {"paper": {k: x.meta.get(k, "") for k in ("title", "sha256", "arxiv_id", "arxiv_version", "source")},
           "source": {k: x.src.get(k, "") for k in ("url", "commit", "discovered_by", "evidence", "status")},
           "repo_is_authors": plan.get("repo_is_authors"), "repo_note": plan.get("repo_note", ""),
           "concerns": sorted(x.concerns(), key=lambda c: (-_RANK[c["severity"]], c["id"])),
           "checks": checks, "central_claims": central,
           "dropped": {"concerns_unresolved_quotes": lens_drops, "checks": plan.get("dropped", [])},
           "scientific_status": scientific_status(x.root)}
    state.write_json(x.root / "ledger.json", out)
    return out


def _cell(s, n: int = 90) -> str:
    s = re.sub(r"\s+", " ", str(s or "")).replace("|", "/")
    return s if len(s) <= n else s[:n - 1] + "…"


def table(led: dict) -> str:
    rows = ["| Check | Kind (evidence) | Bears on | Printed target | Result | Status | Rule or blocker |",
            "|---|---|---|---|---|---|---|"]
    for c in led["checks"]:
        target = c["printed"] or _cell(c["step"] or c["statement"], 60)
        got = "" if not c.get("values") else f"{sum(c['values']) / len(c['values']):.6g} (n={len(c['values'])})"
        rows.append(f"| {c['id']} | {c['kind']} ({c['evidence']}) | {_cell(c['claim'], 70)} | {_cell(target, 40)} | "
                    f"{got} | **{c['status']}** | {_said(_cell(c['rule'] or c['reason'], 160), led)} |")
    if not led["checks"]:
        rows.append("| — | no check was planned | | | | | |")
    rows += ["", "| Central claim | Checks | Claim status | Why unchecked |", "|---|---|---|---|"]
    rows += [f"| {_cell(cc['quote'], 110)} (p{cc['page']}) | {', '.join(cc['checks']) or '—'} | "
             f"{cc['claim_status']} | {_said(_cell(cc['why_unchecked'], 140), led)} |" for cc in led["central_claims"]]
    return "\n".join(rows)


def unearned(text: str, led: dict) -> list[str]:
    """Sentences using a status word (any inflection, through markdown or look-alike
    characters) that no check cited in the same sentence earns. Negated use ("was not
    reproduced", "unverified") is fine."""
    status = {c["id"]: c["status"] for c in led["checks"]}
    # Harness vocabulary (RESOLVED_VERIFIED, AUTHOR_CODE_REPRODUCTION...) names a status; it is not a claim.
    norm = re.sub(r"[*_`~]", "", re.sub(r"\b[A-Z]+(?:_[A-Z]+)+\b", " ", flat_text(text)))
    bad = []
    for sentence in re.split(r"(?<=[.!?])\s+|\n+", norm):
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
             "", f"## Status (computed by the harness): {led['scientific_status']}", "", table(led), ""]
    if fails:
        lines += ["## Established failures", ""] + [
            f"- **{c['id']}** {c['status']} ({c['evidence']}): {c['reason']} — see `ledger.json`, `{c['records'] or 'outcome'}`"
            for c in fails] + [""]
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
