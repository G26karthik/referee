"""The machine ledger, the deterministic status table, and the bounded human review.

Statuses are computed here from outcome files, never taken from a model (invariant 2).
The headline is about the paper's CENTRAL claims only; checks no central claim rests on are
reported apart as incidental, and whether the workflow finished is reported apart again. Four things
are never one: the workflow reached a terminal state, the requested experiment RAN, its protocol
matched what the claim names, and the evidence supports or contradicts the claim. A simulation, a
substituted dataset or a mathematical check beside an empirical claim is supporting evidence with
its own outcome; it never counts as the experiment the claim names.
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
from . import execute
from .execute import EVIDENCE
from .reconcile import FAILURE, PARTIAL, READINGS_DIFFER, SUPPORT

_SUPPORT_WORDS = ("verif(?!ier)", "reproduc", "confirm", "validat(?!ion)", "replicat", "corroborat")  # a validation set is data
_FAILURE_WORDS = ("refut", "disprov", "counterexampl", "contradict", "falsif", "failed to reproduc",
                  "failed reproduc")
# Protocol nouns ("replicate count", "3 seeded replicates") name units of a run, not a replication.
_NOUNS = re.compile(r"\b(?:\d+|seeded|independent|per|of)\s+replicates?\b|\breplicates?\s+(?:count|number)s?\b", re.I)
# A claim about an experiment (a comparison, a printed number, a component that trains) is satisfied
# only by that experiment; the checks of any other role stand beside it.
EMPIRICAL = ("performance", "value", "engineering")
_EXPERIMENT = ("AUTHOR_CODE", "RECONSTRUCTION", "RELEASED_DATA")
_STAGES_SHOWN = 40   # ponytail: stages listed per check in review.md; every stage is in ledger.json
_RANK = {"FATAL": 3, "MAJOR": 2, "MINOR": 1, "NOTE": 0}
_TESTED = SUPPORT + FAILURE + ("NO_VIOLATION_FOUND", READINGS_DIFFER)
# What each evidence basis is, in the reader's words: an audit of published results is not a
# recomputation from predictions, and neither is a fresh run.
BASIS_WORDS = {"published_results": "audit of released result files", "predictions": "recomputed from released predictions",
               "fresh_run": "fresh run", "exact_instances": "exact instances", "paper_numbers": "the paper's own numbers"}


def parts(item: str) -> list[str]:
    """The items of a scope entry that lists several ("A, B and C", "A; B"): split at commas and semicolons outside
    brackets (never inside a number such as 1,000), and the last of them at a final and/or. One item returns itself
    (Oct-01 PPRM: one entry naming five benchmarks read as covered while one of them never arrived)."""
    s, out, depth, cur = str(item or ""), [], 0, ""
    for i, ch in enumerate(s):
        depth += ch in "([{"
        depth -= ch in ")]}" and depth > 0
        if ch in ",;" and depth == 0 and not (ch == "," and 0 < i < len(s) - 1 and s[i - 1].isdigit() and s[i + 1].isdigit()):
            out.append(cur)
            cur = ""
            continue
        cur += ch
    out = [p.strip() for p in out + [cur] if p.strip()]
    if len(out) < 2:
        return [s.strip()]
    if not re.search(r"[(\[{]", out[-1]):
        out = out[:-1] + [p.strip() for p in re.split(r"\s+(?:and|or)\s+", out[-1]) if p.strip()]
    return out


def same_claim(a: str, b: str) -> bool:
    """One printed claim: the same quote, or one quote inside the other (a later round quoting part of a sentence an
    earlier plan listed restates that claim; it never opens a second, narrower one beside it)."""
    fa, fb = flat(a), flat(b)
    return fa == fb or (min(len(fa), len(fb)) >= 20 and (fa in fb or fb in fa))


def withdrawn_plans(root: Path) -> list[dict]:
    """Follow-up plans the operator withdrew (tasks._replan), each still sealed as plan:2.withdrawn.N: their checks were
    set aside, never their claims. A file whose bytes no longer hash to its seal is not read as one."""
    seals = state.read_json(Path(root) / "seals.json", {}) or {}
    out = []
    for f in sorted((Path(root) / "sealed").glob("plan__2.withdrawn.*.json"), key=lambda p: p.stem.rsplit(".", 1)[1].zfill(4)):
        n = f.stem.rsplit(".", 1)[1]
        if seals.get(f"plan:2.withdrawn.{n}") == state.sha256(f.read_bytes()):
            out.append({**(state.read_json(f) or {}), "withdrawn": n})
    return out


def _one(a: dict, b: dict) -> bool:
    """One claim: the same extracted id, or (without ids) the same quote or one inside the other."""
    return a["id"] == b["id"] if a.get("id") and b.get("id") else same_claim(a["quote"], b["quote"])


def claims_of(root: Path) -> list[dict]:
    """The main claims extracted before any plan (sealed/claims.json, only if it still hashes to its seal)."""
    return (sealed_record(root, "claims") or {}).get("claims") or []


def merged(plan: dict | None, follow: dict | None, withdrawn: list[dict] | tuple = (), extracted: list | tuple = ()) -> dict | None:
    """The first plan with its follow-up round: the follow-up's checks appended, its links and
    reasons joined onto the same central claims (matched by extracted id, else by quote, or one quote inside the
    other). A claim a withdrawn follow-up plan listed stays accounted, unchecked, until a later round takes it up, and so
    does every EXTRACTED main claim no plan round took up (a malformed or partial plan never removes one): re-planning a
    round never removes a claim from the record (Oct-01 PPRM)."""
    if not plan:
        return plan
    claims = [dict(cc) for cc in plan["central_claims"]]
    for f in (follow or {}).get("central_claims", []):
        cc = next((c for c in claims if _one(c, f)), None)
        if cc is None:
            claims.append(f)
            continue
        cc["checks"] = cc["checks"] + [k for k in f["checks"] if k not in cc["checks"]]
        cc["why_unchecked"] = f.get("why_unchecked") or cc.get("why_unchecked", "")
        # An omission stays on record until a check that covers it RAN (decided in _completion from what ran),
        # never merely because a follow-up check was planned for it. The later round's word on an item it re-examined
        # is the one reported; the earlier one stays beside it (Oct-01 PPRM: the re-plan's own blocker was dropped).
        om = {flat(o["item"]): o for o in cc.get("omitted") or []}
        for o in f.get("omitted") or []:
            k = flat(o["item"])
            om[k] = {**o, "earlier": {x: om[k][x] for x in ("blocker", "why", "unverified") if om[k].get(x)}} if k in om else o
        cc["omitted"] = list(om.values())
        for k in ("blocker", "not_the_dataset"):
            cc[k] = f.get(k) or cc.get(k, "")
        for k in ("discovery", "failed_checks"):
            cc[k] = list(dict.fromkeys((cc.get(k) or []) + (f.get(k) or [])))
    for w in withdrawn:
        for f in w.get("central_claims") or []:
            if not any(_one(c, f) for c in claims):
                claims.append({**f, "checks": [], "withdrawn_plan": w["withdrawn"], "why_unchecked": (
                    f"listed only by a follow-up plan the operator withdrew (sealed/plan__2.withdrawn.{w['withdrawn']}.json; "
                    f"its checks are kept aside as checks/<id>.withdrawn.{w['withdrawn']}); no later round took it up")})
    for k in extracted:
        if not any(_one(c, k) for c in claims):
            claims.append({**{f: k.get(f) for f in ("id", "quote", "page", "claim_type", "scope", "statement", "assumptions",
                                                    "required_evidence", "interpretations") if k.get(f) is not None},
                           "checks": [], "omitted": [], "blocker": "", "discovery": [], "failed_checks": [],
                           "not_the_dataset": "", "why_unchecked": "no plan round took this extracted main claim up "
                                                                   "(the sealed plan has no entry for it)"})
    out = {**plan, "central_claims": claims}
    return {**out, "checks": plan["checks"] + follow["checks"],
            "dropped": plan.get("dropped", []) + follow.get("dropped", [])} if follow else out


def plan_of(root: Path) -> dict | None:
    """The plan of one review as sealed on disk: the first round, its follow-up, and the claims of any withdrawn one."""
    return merged(state.read_json(Path(root) / "sealed" / "plan.json"), state.read_json(Path(root) / "sealed" / "plan__2.json"),
                  withdrawn_plans(root), claims_of(root))


def _scoped(d: dict, c: dict) -> bool:
    """A claim-changing deviation that holds in one named reading only, while the printed definition is computed beside it
    in the same run (a reading with source 'paper'): it changes that reading, never the check (the readings are decided
    each on its own, and differing ones read READINGS_DIFFER)."""
    return bool(d.get("reading")) and any(r.get("source") == "paper" for r in c.get("reading_defs") or c.get("readings_declared")
                                          or [] if isinstance(r, dict))


def _changed(c: dict) -> bool:
    return any(d.get("changes_claim") and not _scoped(d, c) for d in c.get("deviations") or [])


def _moved_before_audit(c: dict) -> bool:
    """A result about a changed claim by what the check itself records: a claim-changing deviation (a REFEREE-supplied
    criterion is one) or data other than it requested. A certificate separates its readings itself (COUNTEREXAMPLE_FOUND
    is always about the text as printed), so only its audit can move it."""
    return (_changed(c) or bool(c.get("data_changed"))) and c["kind"] != "CERTIFICATE"


def _moved(c: dict) -> bool:
    """Is a check's result about a changed claim rather than the printed one? As recorded by the check, or — for a
    failure — as an independent audit found it: resting on a reading or a choice the paper's words leave open."""
    return _moved_before_audit(c) or (c.get("status") in FAILURE
                                      and (c.get("audit") or {}).get("verdict", "STANDS") != "STANDS")


def sealed_record(root: Path, tid: str) -> dict | None:
    """A sealed task record, only if its bytes still hash to what was sealed (as tasks._sealed)."""
    f = Path(root) / "sealed" / f"{re.sub(r'[^\w.-]+', '__', tid)}.json"
    seal = (state.read_json(Path(root) / "seals.json", {}) or {}).get(tid)
    return state.read_json(f) if seal and f.is_file() and state.sha256(f.read_bytes()) == seal else None


def needs_audit(root: Path, c: dict) -> bool:
    """Does this check's failure stand on the printed claim, so that an independent audit must look at it before it counts?
    A target check whose status is a failure (an arithmetic contradiction has its own page-image transcriber) and whose
    result is not already about a changed claim."""
    o = state.read_json(Path(root) / "checks" / c["id"] / "outcome.json") or {}
    if o.get("status") not in FAILURE or c["kind"] == "ARITHMETIC" or c.get("role", "target") != "target":
        return False
    run = state.read_json(Path(root) / "checks" / c["id"] / "check.json", {}) or {}
    chk = {**c, "deviations": run.get("deviations", []), "data_identity": o.get("data_identity"), "values": o.get("values")}
    return not _moved_before_audit({**chk, "data_changed": _data_changed(chk, Path(root))})


def _data_changed(c: dict, root: Path) -> list[str]:
    """How the data a check ran on differs from what it requested, from the harness's own records: the
    acquisition's gaps (a cut listing, a named file missing or rejected, an include pattern that matched
    nothing), a dataset whose identity line did not affirm the paper's description, or — for a check that
    acquired data — no identity line at all. Empty when nothing says the data changed."""
    if c["kind"] not in _EXPERIMENT:
        return []
    from .execute import data_gaps, unaccounted
    d = state.read_json(root / "checks" / c["id"] / "data.json") or {}
    out = list(data_gaps(d)) + unaccounted(root, d, c["acquire"]) if c.get("acquire") else []
    ident = c.get("data_identity") or {}
    out += [f"dataset {n}: identity not affirmed (matches: {i.get('matches') if isinstance(i, dict) else i!r})"
            for n, i in ident.items() if not (isinstance(i, dict) and i.get("matches") is True)]
    if c.get("acquire") and not ident and c.get("values"):
        out.append("no REFEREE_DATA identity line: the data the run read was never compared with the paper's description")
    elif c.get("acquire") and ident and c.get("values"):
        from .execute import unshown_items
        out += [f"{s}: no REFEREE_DATA line shows the data loaded for it" for s in unshown_items(
            c, [i for i in ident.values() if isinstance(i, dict)])]
    return out


def _empirical(claim_type: str) -> bool:
    """A claim is about an experiment unless it is typed a mathematical statement; an untyped claim
    gets the strictest rules, never none."""
    return claim_type != "theory"


def _claim_status(cs: list[dict], conflicted: bool = False, claim_type: str = "", partial: bool = False) -> str:
    """What the checks of one central claim FOUND. A failed step of a printed proof is a gap in
    the proof, never a refutation of the statement; a premise no tested instance met, or a
    result obtained only after changing what the claim says (a premise, a definition, the
    method's tuning or training data, the data itself...), is neither support nor failure of the printed
    claim; a check whose protocol did not complete is partial evidence, whatever its completed part says,
    and so is support for a claim whose requested scope did not all run (`partial`). Readings of one
    quantity that differ (the paper's definition, the code's) are recorded, not resolved; an engineering
    compatibility test speaks only for an engineering claim; a supporting check never speaks for a claim."""
    cs = [c for c in cs if c.get("role", "target") == "target"]   # a simulation or a proof beside it is not the experiment
    if _empirical(claim_type):     # nor, for a claim about an experiment, exact instances of a lemma (invariant 24)
        cs = [c for c in cs if not (c["kind"] == "CERTIFICATE" or (c["kind"] == "ARITHMETIC" and claim_type != "value"))]
    if not cs:
        return "NOT_CHECKED"
    cs = [c for c in cs if not (c.get("test") == "compatibility" and claim_type != "engineering")]
    if not cs:
        return "NOTHING_DECIDED"
    if conflicted:
        return "CHECKS_DISAGREE"
    # A certificate separates its readings itself (COUNTEREXAMPLE_FOUND is always about the text as
    # printed); any other check's result under a claim-changing deviation, or on data other than the data
    # it requested, is about the changed claim — and so is a failure an independent audit found resting on a reading
    # or choice the paper leaves open.
    st = _found(cs, _moved)
    return "PARTIAL_EVIDENCE" if partial and st in ("SUPPORT_FOUND", "NO_VIOLATION_FOUND") else st


def _found(cs: list[dict], moved) -> str:
    fails = [c for c in cs if c["status"] in FAILURE and not moved(c)]
    held = [c for c in fails if c.get("audit_owed") and not c.get("audit")]   # awaiting its independent audit: not yet one
    fails = [c for c in fails if c not in held]
    if any(c["evidence"] != "PROOF_AUDIT" for c in fails):
        return "FAILURE_FOUND"
    if fails:
        return "PROOF_GAP_FOUND"
    if held:
        return "PENDING"
    if any(c["status"] == READINGS_DIFFER for c in cs):
        return "READINGS_DISAGREE"
    if any(c["status"] in SUPPORT and not moved(c) for c in cs):
        return "SUPPORT_FOUND"
    if any(c["status"] == "PREMISE_NOT_MET" for c in cs):
        return "PREMISE_NOT_MET"
    if any(c["status"] == "VIOLATION_UNDER_CHANGED_READING" or (c["status"] in SUPPORT + FAILURE and moved(c))
           or (c["status"] == "NO_VIOLATION_FOUND" and (_changed(c) or moved(c))) for c in cs):
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
    if o.get("data_blocker"):
        return "DATA_BLOCKED"              # its data was not acquired: no experiment was written or run
    s = o.get("status")
    ex = o.get("execution")
    if ex and s not in ("BLOCKED", "NOT_CHECKABLE"):      # how the runs went, apart from what they found
        if not ex.get("runs_exited_ok") and not o.get("values"):
            return "FAILED"
        if (ex.get("runs_ended") or 0) < (ex.get("runs_planned") or 0) or ex.get("runs_failed"):
            return "PARTIALLY_COMPLETED"
        return "COMPLETED"
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
    extra += "".join(f"; reading {r}: {p['status']}" for r, p in (c.get("readings") or {}).items())
    extra += _measured_before(c) if c.get("status") == "BLOCKED" else ""
    label = f" {c['test']} test" if c.get("test") == "compatibility" else ""
    label += f" ({BASIS_WORDS[c['basis']]})" if c.get("basis") in ("published_results", "predictions") else ""
    return (f"{c['id']} {c['kind']}{label}{' proof step' if c['evidence'] == 'PROOF_AUDIT' else ''} on {what}{stages}"
            f"{' (claim reading changed)' if _changed(c) else ''}: {c['status']}{extra}")


def _unrequested_lines(c: dict) -> list[str]:
    """What the source's complete listing names that this plan did not request: said by the harness, beside the data
    identity lines, so a file left out by the plan never reads as missing from the release."""
    from .fetcher import _named
    out, named = [], {f for u in (c.get("not_requested") or {}).values() for f in u["not_requested"]}
    for d, u in sorted((c.get("not_requested") or {}).items()):
        acq = c.get("acquire") or []
        exc = (acq[int(d)] if d.isdigit() and int(d) < len(acq) else {}).get("exclude") or []
        out.append(f"  - source `{u['source']}`: its record lists {u['listed']} data file(s); this plan did not request: "
                   + ", ".join(u["not_requested"][:12]) + (f" (+{len(u['not_requested']) - 12} more)" if len(u["not_requested"]) > 12 else "")
                   + " — not acquired by this check, not absent from the release")
        out += [f"    - excluded by the plan: {', '.join(n for n in u['not_requested'] if _named(n, [e['pattern']]))[:200]} — "
                f"{_cell(e['why'], 200)}" for e in exc if any(_named(n, [e["pattern"]]) for n in u["not_requested"])]
    for name, ident in (c.get("data_identity") or {}).items():
        src = str((ident or {}).get("source") or "") if isinstance(ident, dict) else ""
        if src.rsplit("/", 1)[-1] in {n.rsplit("/", 1)[-1] for n in named}:
            out.append(f"  - dataset {name} ({src.rsplit('/', 1)[-1]}): the record lists this file; this plan did not request it")
    return out


def _measured_before(c: dict) -> str:
    """What a run printed before a resource limit ended it: kept beside the blocker, deciding nothing."""
    st = c.get("pilot_stages") or {}
    return ("; measured before the limit, deciding nothing: " + ", ".join(f"{s} n={p['n']} mean {p['mean']}"
                                                                         for s, p in st.items())) if st else ""


def _withdrawn(root: Path) -> list[dict]:
    """Checks of a follow-up plan the operator withdrew (checks/<id>.withdrawn.N, see tasks._replan): never a live check,
    always shown beside the report with the operator's stated reason and what they found or measured."""
    why = {str(i + 1): e.get("why", "") for i, e in enumerate(
        e for e in map(json.loads, (root / "log.jsonl").read_text(encoding="utf-8").splitlines() if (root / "log.jsonl").exists() else [])
        if e.get("event") == "replan")}
    out = []
    for d in sorted((root / "checks").glob("*.withdrawn.*")):
        o, n = state.read_json(d / "outcome.json") or {}, d.name.rsplit(".", 1)[1]
        out.append({"folder": f"checks/{d.name}", "plan": f"sealed/plan__2.withdrawn.{n}.json", "kind": o.get("kind", ""),
                    "status": o.get("status") or "NOT_FINISHED", "reason": str(o.get("reason", ""))[:300],
                    "pilot_stages": o.get("pilot_stages") or {}, "withdrawn_because": why.get(n, "")[:500]})
    return out


def counts(c: dict) -> dict:
    """What one check was expected to produce and what it produced, reconciled from its own record: result units
    declared; units with a decided result, undefined (the quantity has no value there) and not completed (no result
    line); the readings each unit was computed under; the processes launched; the result lines; and how many of the
    launches were independent replicates (identical reruns are one). Settings, readings, launches and lines are four
    different numbers and are never added into one (Sep-29 changepoint C7: 124 settings x 2 readings x 3 identical
    launches = 660 lines, 220 distinct comparisons)."""
    st, ex = c.get("stages") or {}, c.get("execution") or {}
    by: dict = {}
    for p in st.values():
        by[p["status"]] = by.get(p["status"], 0) + 1
    undef, missing = by.get("UNDEFINED", 0), by.get("NOT_COMPLETED", 0)
    with_result = len(st) - undef - missing if st else int(bool(c.get("values")))
    declared = set(c.get("units") or [])
    unit_names = set(st) or ({""} if c.get("values") else set())
    lost = sorted(declared - {("" if s == "(unnamed)" else s) for s in unit_names})
    ind = [p.get("n_independent", p.get("n")) for p in st.values() if p["status"] not in ("UNDEFINED", "NOT_COMPLETED")
           and isinstance(p.get("n_independent", p.get("n")), int)]
    if not st and isinstance(c.get("n_independent", c.get("n")), int):
        ind = [c.get("n_independent", c.get("n"))]
    readings = sorted(c.get("readings") or {}) if isinstance(c.get("readings"), dict) else []
    units = len(declared) if declared else len(unit_names)
    return {"units_declared": units, "units_with_result": with_result, "units_undefined": undef,
            "units_not_completed": missing + len(lost), "units_unaccounted": lost[:20], "readings": readings,
            "comparisons": with_result * max(1, len(readings)), "launches": ex.get("runs_ended", c.get("runs") or 0),
            "launches_planned": ex.get("runs_planned"), "result_lines": len(c.get("values") or []),
            "independent_replicates": min(ind) if ind else None,
            "reconciles": units == with_result + undef + missing + len(lost) and not lost}


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
                    "covers": c.get("covers", []), "acquire": c.get("acquire", []), "role": c.get("role", "target"),
                    "data_blocker": o.get("data_blocker"),
                    "test": c.get("test", ""), "basis": c.get("basis", ""), "define": c.get("define"),
                    "reading_defs": run.get("readings") or c.get("readings") or [], "readings": o.get("readings"),
                    "execution": o.get("execution"), "stochastic": run.get("stochastic"),
                    "command": run.get("command", ""), "identity": run.get("identity"),
                    "script_sha256": run.get("script_sha256", ""), "deviations": run.get("deviations", []),
                    "revisions": run.get("revisions") or [], "criterion": run.get("criterion", c.get("criterion", "")),
                    "premise_argument": run.get("premise_argument", ""),
                    "state": _state(root, c["id"], state.read_json(cdir / "outcome.json", {}) or {}),
                    # an outcome decided from checkpointed seeds before they counted as runs says so in its protocol
                    "runs": o.get("runs") or (o.get("protocol") or {}).get("seeds_reused_from_checkpoints"),
                    "values": o.get("values"), "literal": o.get("literal"),
                    "admissible": o.get("admissible"), "protocol": o.get("protocol"),
                    "image_check": o.get("image_check"), "pilot_values": o.get("pilot_values"), "pilot_stages": o.get("pilot_stages"),
                    "status": o.get("status"), "reason": o.get("reason", ""), "reason_by": o.get("reason_by", "harness"),
                    "rule": o.get("rule") or next((p["rule"] for p in (o.get("stages") or {}).values() if p.get("rule")), ""),
                    "stages": o.get("stages"), "status_on_completed": o.get("status_on_completed"),
                    "completed_stages": o.get("completed_stages"),
                    "failed_seeds": o.get("failed_seeds"), "data_identity": o.get("data_identity"), "data": o.get("data"),
                    "not_requested": execute.unrequested(root, state.read_json(cdir / "data.json") or {}),
                    "resource": o.get("resource"), "stage_times": o.get("stage_times"), "peak_mb": o.get("peak_mb"),
                    "environment": o.get("environment"), "authorization": o.get("authorization", ""),
                    "commit": o.get("commit", ""), "history": history,
                    "records": "execution.jsonl" if o.get("runs") or (o.get("protocol") or {}).get(
                        "seeds_reused_from_checkpoints") else ""})
        out[-1]["data_changed"] = _data_changed(out[-1], root)
        # A failure about the printed claim counts once an independent auditor, shown the failing instances, found it
        # does not rest on a reading or choice the paper's words leave open (tasks._audits; it can only lower).
        out[-1]["audit_owed"] = needs_audit(root, c)
        out[-1]["audit"] = sealed_record(root, f"audit:{c['id']}") if out[-1]["audit_owed"] else None
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
    """Each central claim with what its requested experiment did (completion) and, from both, what it found:
    support stands only for a claim whose requested scope all ran; on a claim whose experiment did not run,
    what else was checked is partial evidence at most."""
    by_id, conflicted = {c["id"]: c for c in checks}, {k for x in conf for k in x["checks"]}
    out = []
    for cc in plan["central_claims"]:
        row = _completion_row(cc, by_id)
        partial = row["partial"] or (_empirical(cc.get("claim_type", "")) and row["experiment"] == "NOT_RUN")
        st = _claim_status([by_id[k] for k in cc["checks"] if k in by_id], bool(conflicted & set(cc["checks"])),
                           cc.get("claim_type", ""), partial)
        out.append({**cc, "statuses": {k: by_id[k]["status"] for k in cc["checks"] if k in by_id},
                    "supporting": [k for k in cc["checks"] if k in by_id and by_id[k].get("role") == "supporting"],
                    "claim_status": st, "completion": {**row, "evidence": st},
                    "deviations": sum(len(by_id[k]["deviations"]) for k in cc["checks"] if k in by_id)})
    return out


# What kept an experiment from running, in the harness's own words. A configured cap of this run (a time budget, the data
# cap, a denied source, a closed gate) is no measurement of what the experiment needs (`cap`); a fault of this run's
# network, host or code says nothing about the data (`fault`); only a measured limit of this host is `compute`, and only a
# fact about the source (missing, inaccessible, invalid, empty) is `data` (Oct-01 PPRM: "compute 3" for one per-run limit).
CAP_RESOURCES = ("time_budget", "per_run_timeout")
DATA_FACTS = ("missing", "inaccessible", "content_invalid", "no_data", "protocol")
RUN_FAULTS = ("transient", "infrastructure", "bug")


def resource_word(resource: str | None) -> str:
    return "cap" if resource in CAP_RESOURCES else "compute"


def data_word(blk: list[dict]) -> str:
    cls = {b.get("class") for b in blk}
    if cls & set(DATA_FACTS):
        return "data"
    capped = [b for b in blk if b.get("class") in ("denied", "gate") or (b.get("class") == "storage" and "SH_MAX_DATA_GB" in str(
        b.get("detail") or ""))]
    if capped:
        return "cap"
    return "compute" if "storage" in cls else "fault"


def _blockers(cc: dict, cs: list[dict], unrun: list[str] = (), done: set = frozenset(), said: set = frozenset()) -> list[dict]:
    """What kept a claim's requested experiment from running, each with who says so: the harness (a recorded
    data blocker, a measured resource limit, a refusal, a scope item whose check did not complete) or the planner
    (an omission's stated reason, with what the seal could not verify of it). An omission that a COMPLETED target
    check covers (`done`: a follow-up round ran it) is no reason anything did not run (Oct-02 PPRM review)."""
    out = [{"item": o["item"], "blocker": o.get("blocker") or "unstated", "why": o["why"][:300], "basis": o.get("basis") or "planner",
            **({"searched": o["discovery"]} if o.get("discovery") else {}),
            **({"unverified": o["unverified"]} if o.get("unverified") else {}),
            **({"budget_spent": o["budget_spent"]} if o.get("budget_spent") else {}),
            **({"cap": o["cap"]} if o.get("cap") else {})} for o in cc.get("omitted") or []
           if o.get("why") and flat(o["item"]) not in done]
    if not cc["checks"] and cc.get("withdrawn_plan"):
        out.append({"item": "(the whole claim)", "blocker": "withdrawn", "why": cc["why_unchecked"][:300], "basis": "operator"})
    elif not cc["checks"] and cc.get("why_unchecked"):
        out.append({"item": "(the whole claim)", "blocker": cc.get("blocker") or "unstated", "why": cc["why_unchecked"][:300],
                    "basis": "planner", **({"searched": cc["discovery"]} if cc.get("discovery") else {}),
                    **({"unverified": cc["unverified"]} if cc.get("unverified") else {}),
                    **({"budget_spent": cc["budget_spent"]} if cc.get("budget_spent") else {})})
    for c in cs:
        if c.get("role", "target") != "target":
            continue
        if c.get("data_blocker"):
            out.append({"item": c["id"], "blocker": data_word(c["data_blocker"]),
                        "class": ", ".join(sorted({b["class"] for b in c["data_blocker"]})), "why": c["reason"][:300],
                        "basis": "harness"})
        elif c["status"] == "BLOCKED":
            out.append({"item": c["id"], "blocker": resource_word(c.get("resource")) if str(c["reason"]).startswith(
                "RESOURCE BLOCKER") else "refused", "class": c.get("resource") or "", "why": c["reason"][:300], "basis": "harness"})
        elif c["status"] == "NOT_CHECKABLE":
            out.append({"item": c["id"], "blocker": "protocol", "why": c["reason"][:300], "basis": "script author or verifier"})
        elif c["status"] in ("INCONCLUSIVE", "PENDING") and not c.get("values"):
            out.append({"item": c["id"], "blocker": "failed" if c["status"] == "INCONCLUSIVE" else "pending",
                        "why": c["reason"][:300], "basis": "harness"})
    named = {flat(b["item"]) for b in out}
    out += [{"item": s, "blocker": "not run", "basis": "harness",
             "why": "its check's own REFEREE_DATA line names it missing: not loaded, so not run" if flat(s) in said
             else "no target check covering it ran to completion"} for s in unrun if flat(s) not in named]
    return out


def _completion_row(cc: dict, by_id: dict) -> dict:
    """For one central claim: did the requested experiment RUN, on what it requested, over its whole scope?
    Derived from harness facts only — check states, the kinds that can run an experiment, recorded deviations,
    the acquisition's own record and the data identity lines — never from a label alone. A supporting check
    stands beside the claim and runs nothing of it."""
    empirical = _empirical(cc.get("claim_type", ""))
    cs = [by_id[k] for k in cc["checks"] if k in by_id]
    target = [c for c in cs if c.get("role", "target") == "target"]
    ran = [c for c in target if c["state"] in ("COMPLETED", "PARTIALLY_COMPLETED")
           and (c["kind"] in _EXPERIMENT if empirical else True) and (c.get("values") or c["kind"] == "ARITHMETIC")]
    changes = [d["used"] for c in ran for d in c["deviations"] if d.get("changes_claim") and not _scoped(d, c)] + [
        f"{c['id']}: {g}" for c in ran for g in c.get("data_changed") or []]
    mismatch = [n for c in ran for n, i in (c.get("data_identity") or {}).items()
                if not (isinstance(i, dict) and i.get("matches") is True)]
    # A covered item the check's own REFEREE_DATA line names missing (`missing`, `not_available`) did not run, whatever
    # `covers` says (Oct-01 PPRM: Social-IQA, never acquired, read as covered).
    said = {flat(m) for c in ran for i in (c.get("data_identity") or {}).values() if isinstance(i, dict)
            for box in (i, i.get("observed")) if isinstance(box, dict)                # the line itself, or what it observed
            for k in ("missing", "not_available") for m in (box.get(k) or []) if isinstance(m, str) and len(flat(m)) >= 3}
    entries = [s for c in ran if c["state"] == "COMPLETED" for s in c.get("covers") or []]
    done = ({flat(p) for s in entries for p in parts(s)} | {flat(s) for s in entries if not {flat(p) for p in parts(s)} & said}) - said
    scope = cc.get("scope") or []
    unrun = [s for s in scope if flat(s) not in done] if scope else [o["item"] for o in cc.get("omitted") or [] if o.get("why")]
    unran = [c["id"] for c in target if c not in ran]
    partial = bool(unrun or unran) or any(c["state"] == "PARTIALLY_COMPLETED" or c["status"] == PARTIAL for c in ran)
    if not ran:
        exp, matched = "NOT_RUN", None
    elif changes:
        exp, matched = "RAN_WITH_CHANGES", False
    elif partial:
        exp, matched = "RAN_PARTIAL", False           # protocol matched includes its scope (invariant 24)
    else:
        exp, matched = "RAN_AS_SPECIFIED", True
    return {"claim": cc["quote"], "page": cc["page"], "claim_type": cc.get("claim_type") or "",
            "requested": "a mathematical statement on exact instances" if not empirical else "an experiment",
            "experiment": exp, "protocol_matched": matched, "partial": partial, "changes": changes[:6],
            "data_mismatch": mismatch, "scope_not_run": unrun, "targets_not_run": unran, "ran": [c["id"] for c in ran],
            "supporting": [{"check": c["id"], "kind": c["kind"], "status": c["status"], "state": c["state"]}
                           for c in cs if c.get("role") == "supporting" or (empirical and c["kind"] == "CERTIFICATE")],
            "not_run": _blockers(cc, cs, unrun, done, said) if exp != "RAN_AS_SPECIFIED" else []}


def _completion(checks: list[dict], claims: list[dict]) -> dict:
    """Per central claim, four things kept apart: did the requested experiment RUN, did its protocol match
    what the claim names, what does the evidence say, and what stood beside it as supporting evidence."""
    rows = [cc["completion"] for cc in claims]
    counts: dict = {}
    for r in rows:
        k = ("experiments " if _empirical(r["claim_type"]) else "statements ") + r["experiment"]
        counts[k] = counts.get(k, 0) + 1
    return {"claims": rows, "counts": counts,
            "workflow": {"terminal": len([c for c in checks if c["status"] != "PENDING"]), "planned": len(checks),
                         "note": "a check in a terminal state was processed by the workflow; that says nothing about whether "
                                 "its experiment ran"}}


def _completion_line(comp: dict) -> str:
    emp = [r for r in comp["claims"] if _empirical(r["claim_type"])]
    word = {"RAN_AS_SPECIFIED": "ran as specified", "RAN_PARTIAL": "ran in part", "RAN_WITH_CHANGES": "ran with claim-changing "
            "changes (about the changed claim)", "NOT_RUN": "not run"}
    parts = [f"{sum(1 for r in emp if r['experiment'] == k)} {v}" for k, v in word.items() if any(r["experiment"] == k for r in emp)]
    blocked: dict = {}
    for r in emp:
        for b in r["not_run"]:
            blocked[b["blocker"]] = blocked.get(b["blocker"], 0) + 1
    th = [r for r in comp["claims"] if not _empirical(r["claim_type"])]
    ran_th = sum(1 for r in th if r["experiment"] != "NOT_RUN")
    w = comp["workflow"]
    ev: dict = {}
    for r in comp["claims"]:
        ev[r.get("evidence", "")] = ev.get(r.get("evidence", ""), 0) + 1
    return (f"central claims: {len(comp['claims'])}, evidence " + ", ".join(f"{v} {k}" for k, v in sorted(ev.items()))
            + f"; requested experiments (central empirical claims): {len(emp)}" + (f" — {', '.join(parts)}" if parts else "")
            + (f" [not run or in part, by blocker: {', '.join(f'{k} {v}' for k, v in sorted(blocked.items()))}]" if blocked else "")
            + f"; mathematical claims: {ran_th} of {len(th)} checked on exact instances; workflow: {w['terminal']} of "
              f"{w['planned']} planned checks reached a terminal state (not a measure of what was reproduced)")


def completion_line(root: Path) -> str:
    plan = plan_of(root)
    if plan is None:
        return "NOT_ASSESSED"
    checks = _checks(root, plan)
    return _completion_line(_completion(checks, _central(plan, checks, conflicts(checks))))


def _headline(checks: list[dict], claims: list[dict]) -> str:
    """The paper-level status: about central claims only (incidental checks never lift it)."""
    st = [c["claim_status"] for c in claims]
    if any(c["status"] == "PENDING" for c in checks) or "PENDING" in st:
        return "CHECKS_PENDING"
    for found, head in (("FAILURE_FOUND", "CENTRAL_FAILURE_FOUND"), ("CHECKS_DISAGREE", "CENTRAL_CHECKS_DISAGREE"),
                        ("PROOF_GAP_FOUND", "CENTRAL_PROOF_GAP_FOUND"),
                        ("READINGS_DISAGREE", "CENTRAL_READINGS_DISAGREE")):
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


def progress(root: Path) -> str:
    """Whether the review of one paper FINISHED: its report is sealed and review.md was written after every
    outcome and seal — otherwise it is in progress (or was interrupted), whatever an older ledger says."""
    seals = state.read_json(root / "seals.json", {}) or {}
    review = root / "review.md"
    newer = [f for f in [root / "seals.json", *root.glob("checks/*/outcome.json")] if f.exists()]
    if "report" in seals and review.exists() and all(f.stat().st_mtime <= review.stat().st_mtime + 1 for f in newer):
        return "FINISHED"
    why = ("no plan sealed" if "plan" not in seals else "the report is not sealed" if "report" not in seals
           else "outcomes or seals are newer than review.md")
    return f"IN PROGRESS ({why}; a ledger or review written earlier is not the current state)"


def scientific_status(root: Path) -> str:
    """What the review CHECKED about the central claims — separate from workflow completion."""
    plan = plan_of(root)
    if plan is None:
        return "NOT_ASSESSED"
    checks = _checks(root, plan)
    return _headline(checks, _central(plan, checks, conflicts(checks)))


def definition_choices(checks: list[dict]) -> list[dict]:
    """One printed sentence of the paper (a filter, a definition, a convention) that several checks applied
    differently: each re-found the same words and recorded a different choice. The harness does not pick one;
    it shows them side by side (a follow-up may compute both as readings in one check). Without author code
    this is the deterministic trace of a paper-vs-released-artifact disagreement the checks ran into."""
    by: dict = {}
    for c in checks:
        for d in c.get("deviations") or []:
            if d.get("printed"):
                g = by.setdefault(flat(d["printed"]), {"printed": d["printed"], "page": d.get("page"), "uses": {}})
                g["uses"].setdefault((flat(d.get("used", ""))[:200], bool(d.get("changes_claim"))), []).append(c["id"])
    return [{"printed": g["printed"][:400], "page": g["page"],
             "choices": [{"checks": sorted(set(ids)), "used": next(d["used"] for c in checks if c["id"] in ids
                                                                  for d in c["deviations"] if flat(d.get("used", ""))[:200] == u
                                                                  and flat(d.get("printed", "")) == k)[:300],
                          "changes_claim": ch} for (u, ch), ids in g["uses"].items()]}
            for k, g in by.items() if len(g["uses"]) > 1 and len({i for ids in g["uses"].values() for i in ids}) > 1]


def _workflow(checks: list[dict]) -> dict:
    by = lambda *s: [c["id"] for c in checks if c["status"] in s]
    return {"checks_planned": len(checks), "finished": len([c for c in checks if c["status"] != "PENDING"]),
            "reached_terminal_state": len([c for c in checks if c["status"] != "PENDING"]),
            "reached_a_scientific_status": by(*_TESTED), "blocked": by("BLOCKED"), "partial": by(PARTIAL),
            "inconclusive": by("INCONCLUSIVE"), "not_checkable": by("NOT_CHECKABLE"),
            "readings_differ": by(READINGS_DIFFER),
            "result_schema_defects": [c["id"] for c in checks if (c.get("execution") or {}).get("schema_defects")],
            "states": {c["id"]: c.get("state") for c in checks},
            "environment_recoveries": {c["id"]: c["environment"]["recovery"] for c in checks
                                       if (c.get("environment") or {}).get("recovery")}}


def _failed_searches(root: Path, plan: dict) -> None:
    """A blocker sealed on searches that all failed (no registry answered) rests on nothing, and a `data` or `compute`
    blocker resting only on checks that stopped at a configured cap or on a fault of this run rests on no fact about the
    data or the host: each is flagged unverified, whatever the seal accepted when it was sealed (a plan sealed before
    the rule, or leniently on its last attempt; Oct-01 PPRM: "compute" citing a per-run timeout of another item)."""
    f = root / "discovery.jsonl"
    recs = {r["id"]: r for r in (json.loads(ln) for ln in f.read_text(encoding="utf-8").splitlines() if ln.strip())
            if "results" in r} if f.exists() else {}
    covers = {c["id"]: {flat(p) for s in c.get("covers") or [] for p in parts(s)} for c in plan.get("checks") or []}
    for cc in plan.get("central_claims") or []:
        for o in [*(cc.get("omitted") or []), cc]:
            ids = [d for d in o.get("discovery") or [] if d in recs]
            bad = [f"{d} {reg}" for d in ids for reg, v in recs[d]["results"].items() if v.get("error")]
            if o.get("blocker") in ("data", "credentials", "compute") and bad:
                o["unverified"] = (o.get("unverified") or f"search(es) it cites failed ({', '.join(bad[:6])}): a registry "
                                   "that did not answer shows nothing about the dataset or model")
            cited = [str(k) for k in o.get("failed_checks") or []]
            if o.get("blocker") not in ("data", "compute") or not cited or o.get("unverified"):
                continue
            words = {}
            for k in cited:
                out = state.read_json(root / "checks" / k / "outcome.json") or {}
                ran_it = not o.get("item") or k not in covers or flat(o["item"]) in covers[k]
                words[k] = ("not this item" if not ran_it else data_word(out["data_blocker"]) if out.get("data_blocker")
                            else resource_word(out.get("resource")) if str(out.get("reason", "")).startswith("RESOURCE BLOCKER")
                            else "nothing")
            if not any(w == o["blocker"] for w in words.values()):
                o["unverified"] = ("the check(s) it cites show no such fact: " + ", ".join(
                    f"{k}: {w if w != 'cap' else 'a configured cap of this run'}" for k, w in words.items()))[:400]


def ledger(x) -> dict:
    """Every report claim joined to its machine artifact; written to ledger.json."""
    plan = x.plan() or {"checks": [], "central_claims": [], "dropped": []}
    _failed_searches(x.root, plan)
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
           "completion": _completion(checks, central), "definition_choices": definition_choices(checks),
           "conflicts": conf, "workflow": _workflow(checks), "withdrawn_checks": _withdrawn(x.root),
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
        if c.get("readings"):                        # each definition's own result, side by side
            got = "; ".join(f"{r}: " + (f"{p['reproduced']:g}" if isinstance(p.get("reproduced"), (int, float)) else
                                        f"margin {p['margin']:g}" if isinstance(p.get("margin"), (int, float)) else
                                        p["status"]) for r, p in c["readings"].items())
        elif c.get("stages"):
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
        ev = c["evidence"] + (f"; {BASIS_WORDS[c['basis']]}" if c.get("basis") in ("published_results", "predictions")
                              else "") + ("; compatibility test" if c.get("test") == "compatibility" else "")
        state_ = c.get("state", "") + ("; result-schema defect" if (c.get("execution") or {}).get("schema_defects") else "")
        rows.append(f"| {c['id']} | {c['kind']} ({ev}) | {_cell(c['claim'], 70)} | {_cell(target, 40)} | "
                    f"{_cell(got, 80)} | **{c['status']}** ({state_}) | {len(c['deviations']) or ''} | "
                    f"{why if rule else _said(why, led)} |")
    return rows


def table(led: dict) -> str:
    """The deterministic status block: central claims, their checks, incidental checks, and
    workflow completion, each apart."""
    by_id = {c["id"]: c for c in led["checks"]}
    comp = led.get("completion") or {"claims": [], "workflow": {"terminal": 0, "planned": 0}}
    head = ["Completion, kept apart from what was found: " + _completion_line(comp), ""] if comp["claims"] else []
    head += [f"- {_cell(r['claim'], 90)} (p{r['page']}, {r['claim_type'] or 'untyped'}): experiment **{r['experiment']}**, protocol "
             f"matched: {'yes' if r['protocol_matched'] else 'no' if r['protocol_matched'] is False else 'n/a (not run)'}, "
             f"evidence {r['evidence']}"
             + (f"; changes: {_cell('; '.join(r['changes']), 160)}" if r["changes"] else "")
             + (f"; data identity mismatch: {', '.join(r['data_mismatch'])}" if r["data_mismatch"] else "")
             + "".join(f"; not run — {b['item']}: {b['blocker']}" + (f" ({b['class']})" if b.get("class") else "")
                       + f" [{b['basis']}" + (f"; NOT verified: {_cell(b['unverified'], 120)}" if b.get("unverified") else "")
                       + (f"; the search budget was spent when this was planned ({b['budget_spent']} searches, "
                          "SH_MAX_DISCOVERIES): a setting of this run, not evidence that anything named is absent"
                          if b.get("budget_spent") else "")
                       + (f"; a configured cap of this run, not a property of the experiment: {_cell(b['cap'], 160)}"
                          if b.get("cap") else "; a configured cap of this run, not a property of the experiment"
                          if b["blocker"] == "cap" else "")
                       + ("; a fault of this run's network, host or code: nothing about the data" if b["blocker"] == "fault"
                          else "")
                       + "]" for b in r["not_run"][:6])
             + "".join(f"; supporting only: {x['check']} {x['kind']} {x['status']}" for x in r["supporting"])
             for r in comp["claims"]]
    head += [""] if head else []
    rows = head + ["| Central claim | What was done | Found | Deviations | Why unchecked |", "|---|---|---|---|---|"]
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
    rows += ["", f"Workflow completion: {w['finished']} of {w['checks_planned']} planned checks reached a terminal state "
                 "(a blocked, refused or inconclusive check is terminal; this is not what was reproduced); "
                 f"{len(w['reached_a_scientific_status'])} reached a scientific status "
                 f"({', '.join(w['reached_a_scientific_status']) or 'none'}); partial: {', '.join(w['partial']) or 'none'}; "
                 f"blocked: {', '.join(w['blocked']) or 'none'}; readings differ: "
                 f"{', '.join(w.get('readings_differ') or []) or 'none'}; "
                 f"inconclusive: {', '.join(w['inconclusive']) or 'none'}; not checkable: "
                 f"{', '.join(w['not_checkable']) or 'none'}."]
    return "\n".join(rows)


def unearned(text: str, led: dict) -> list[str]:
    """Sentences using a status word (any inflection, through markdown or look-alike
    characters) that no check cited in the same sentence earns. Negated use ("was not
    reproduced", "unverified", "nothing here shows that any result was reproduced") is fine. A support
    word is earned only by a check whose own result is about the printed claim (no claim-changing deviation,
    no change of data) and whose every central claim found support with its requested experiment run as
    specified; a failure word only by a failure about the printed claim."""
    by_id = {c["id"]: c for c in led["checks"]}
    claims_of: dict = {}
    for cc in led.get("central_claims") or []:
        for k in cc.get("checks") or []:
            claims_of.setdefault(k, []).append(cc)
    def earns(i: str, statuses: tuple) -> bool:
        c = by_id.get(i)
        if not c or c["status"] not in statuses or _moved(c) or (c.get("audit_owed") and not c.get("audit")):
            return False
        return statuses is FAILURE or (c.get("role", "target") == "target" and all(
            cc.get("claim_status") == "SUPPORT_FOUND"
            and (cc.get("completion") or {}).get("experiment", "RAN_AS_SPECIFIED") == "RAN_AS_SPECIFIED"
            for cc in claims_of.get(i, [])))
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
                start = max([low.rfind(p, 0, m.start()) for p in (";", ":", ",", "—", "–", "(")] + [-1]) + 1
                neg = r"\b(?:not|no|never|cannot|without|nothing|neither|nor|none)\b|n't"
                negated = m.group().startswith("un") or re.search(neg, low[start:m.start()]) or re.search(
                    neg, low[max(0, m.start() - 30):m.start()])          # the clause the word stands in, or just before it
                if not negated and not any(earns(i, earned) for i in ids):
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
    # Three kinds of failure status, never one list (Oct-02 transformer: C2/C3/C8, about REFEREE's own criterion, were
    # listed as "Failures found" beside C6): of the printed claim (audited where owed); resting on a reading or choice the
    # paper leaves open (its audit); and of a changed claim (a supplied criterion, a claim-changing deviation, other data).
    allf = [c for c in led["checks"] if c["status"] in FAILURE]
    fails = [c for c in allf if not _moved(c) and not (c.get("audit_owed") and not c.get("audit"))]
    rests = [c for c in allf if not _moved_before_audit(c) and _moved(c)]
    changed = [c for c in allf if _moved_before_audit(c)]
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
               else "") + f"{c['reason']} — see `ledger.json`, `{c['records'] or 'outcome'}`"
            + (" (an independent audit found it stands on the paper's own words)" if c.get("audit") else "") for c in fails] + [""]
    if rests:
        lines += ["## Failures that rest on a reading or choice the paper leaves open (independent audit; for a human)", ""]
        for c in rests:
            a = c.get("audit") or {}
            lines.append(f"- **{c['id']}** {c['status']} ({c['evidence']}): {_cell(c['reason'], 200)} — not counted as a "
                         "failure of the printed claim: " + ("the independent audit could not be completed"
                                                             if a.get("verdict") == "UNRESOLVED" else "it rests on:"))
            lines += [f"  - " + (f"\"{_cell(d['printed'], 160)}\" (p{d['page']})" if d.get("printed") else
                                 f"declared deviation {d.get('deviation')}") + f": tested as {_cell(d['tested_as'], 160)}; "
                      f"the claim may hold under: {_cell(d['alternative'], 200)}" for d in a.get("depends_on") or []]
        lines.append("")
    if changed:
        lines += ["## Violations of a changed claim (a REFEREE-supplied criterion, a claim-changing deviation, or other data)",
                  ""] + [f"- **{c['id']}** {c['status']} ({c['evidence']}): {_cell(c['reason'], 200)} — about the changed "
                         "claim, never a failure of the claim as printed: " + _cell("; ".join(
                             [d["used"] for d in c["deviations"] if d.get("changes_claim")] + list(c.get("data_changed") or [])), 300)
                         for c in changed] + [""]
    if led["conflicts"]:
        lines += ["## Conflicting readings of one printed object (recorded, not resolved)", ""]
        for cf in led["conflicts"]:
            lines.append(f"- {_cell(cf['object'], 200)} — {cf['note']}")
            lines += [f"  - {r['check']} {r['reading']}: **{r['result']}**" + (f" ({r['instances']})" if r.get("instances") else "")
                      + "".join(f"; printed \"{_cell(d['printed'], 80)}\" -> used: {_cell(d['used'], 120)}"
                                for d in r["deviations"]) for r in cf["readings"]]
        lines.append("")
    if led.get("definition_choices"):
        lines += ["## One printed definition, several choices (checks that applied the same sentence differently; "
                  "not resolved)", ""]
        for d in led["definition_choices"]:
            lines.append(f"- \"{_cell(d['printed'], 200)}\" (p{d['page']})")
            lines += [f"  - {', '.join(u['checks'])}: {_cell(u['used'], 200)}"
                      + (" **(changes the claim)**" if u["changes_claim"] else "") for u in d["choices"]]
        lines.append("")
    two = [c for c in led["checks"] if c.get("reading_defs")]
    if two:
        lines += ["## One quantity, several definitions (each computed on the same runs, data and cohort; not resolved)", ""]
        for c in two:
            lines.append(f"- **{c['id']}** {c['status']}: " + _said(_cell(c["reason"], 300), led))
            for r in c["reading_defs"]:
                got = (c.get("readings") or {}).get(r["name"]) or {}
                where = f"the paper (p{r.get('page')})" if r["source"] == "paper" else f"`{r['source']}`"
                lines.append(f"  - reading **{r['name']}**, as defined in {where}: \"{_cell(r['quote'], 160)}\""
                             + (f" -> {got.get('status')}" + (f", {got['reproduced']:g}" if isinstance(
                                 got.get("reproduced"), (int, float)) else "") if got else ""))
        lines.append("")
    schema = [c for c in led["checks"] if (c.get("execution") or {}).get("schema_defects")]
    if schema:
        lines += ["## Execution and result schema (how the runs went, apart from what they found)", ""]
        for c in schema:
            ex = c["execution"]
            lines.append(f"- **{c['id']}** {c.get('state')}: {ex['runs_exited_ok']} of {ex['runs_planned']} planned run(s) "
                         f"exited cleanly; status {c['status']}. Result-schema defect(s), the script's labeling, not a "
                         "finding about the paper: " + "; ".join(_cell(d, 200) for d in ex["schema_defects"][:5]))
        lines.append("")
    devs = [(c["id"], d) for c in led["checks"] for d in c["deviations"]]
    recs = led["workflow"]["environment_recoveries"]
    revs = [(c["id"], rv) for c in led["checks"] for rv in c.get("revisions") or []]
    if devs or recs or revs:
        lines += ["## Recorded deviations, revisions and environment recoveries", ""] + [
            f"- {cid}{' **(changes the claim)**' if d.get('changes_claim') else ''}"
            + (" (so judged by the independent verifier)" if d.get("changes_claim_by") == "verifier" else "") + ": "
            + (f"printed \"{_cell(d['printed'], 120)}\" (p{d['page']})" if d["printed"] else "the paper is silent")
            + f" -> used: {_cell(d['used'], 200)}. Why: {_cell(d['why'], 200)}" for cid, d in devs] + [
            f"- {cid} revision of round {rv['from_round']}: " + "; ".join(
                f"\"{_cell(n['was'], 100)}\" — {_cell(n['why'], 200)}" for n in rv.get("notes") or [])
            for cid, rv in revs if rv.get("notes")] + [
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
                got = next((a for a in (c.get("acquire") or []) if a.get("source") == src.get("source")), {})
                last = (src.get("attempts") or [{}])[0]
                lines.append(f"- **{c['id']}** source `{src.get('source')}`"
                             + (f" (cited as \"{_cell(got['cited_as'], 80)}\", {got.get('cited_form')})" if got.get("cited_as") else "")
                             + (f" (discovered by search {got['discovery']}: {got.get('found', {}).get('title', '')[:60]!r})"
                                if got.get("discovery") else "")
                             + (f" -> {last['final_url']}" if last.get("final_url") and last.get("final_url") != src.get("source") else "")
                             + (f" (HTTP {last['http_status']})" if last.get("http_status") else "")
                             + (f" revision `{src['revision'][:12]}`" if src.get("revision") else "")
                             + (f"; {src['admitted_files']} file(s) admitted" if "admitted_files" in src else "")
                             + (f"; TRUNCATED: {src['truncated']['matched']} matched, {src['truncated']['followed']} followed"
                                if src.get("truncated") else "")
                             + (f" — NOT ACQUIRED [{src['failure_class']}]" if src.get("failure_class") else "")
                             + (f" — ERROR: {_cell(src['error'], 200)}" if src.get("error") else "")
                             + "".join(f"; rejected {r.get('file')} ({r.get('class')}: {_cell(r.get('why'), 80)})"
                                       for r in (src.get("rejected") or [])[:4])
                             + "".join(f"; recovery: {_cell(r, 100)}" for r in (src.get("recovery") or [])[:4])
                             + "".join(f"; followed {f.get('url')}" + (f" ERROR [{f.get('class')}] {_cell(f['error'], 80)}"
                                                                        if f.get("error") else "") for f in (src.get("followed") or [])[:5]))
            if d.get("n_files") is not None:
                lines.append(f"  - {d.get('n_files')} files, {d.get('bytes')} bytes, each with sha256 in "
                             f"`checks/{c['id']}/data.json` (fetched {d.get('fetched_at', '?')})")
            for name, ident in (c.get("data_identity") or {}).items():
                lines.append(f"  - dataset {name}: {_said(_cell(json.dumps(ident, ensure_ascii=False), 300), led)}")
            lines += _unrequested_lines(c)
        lines.append("")
    hist = [(c["id"], h) for c in led["checks"] for h in c.get("history") or []]
    wd = led.get("withdrawn_checks") or []
    if hist or wd:
        lines += ["## Superseded and withdrawn results (an earlier run of a check; a check of a withdrawn follow-up plan, "
                  "whose id a later round may reuse for another check)", ""] + [
            f"- {cid} `{h['file']}`: {h['status']} — {_cell(h['reason'], 200)}" for cid, h in hist] + [
            f"- `{w['folder']}` (its follow-up plan `{w['plan']}` was withdrawn by the operator: "
            f"{_cell(w['withdrawn_because'], 240)}): {w['status']} — {_cell(w['reason'], 200)}{_measured_before(w)}"
            for w in wd] + [""]
    prot = [(c["id"], c["protocol"]) for c in led["checks"] if c.get("protocol")]
    if prot:
        lines += ["## Protocol choices (what the paper stated, what REFEREE supplied)", ""]
        for cid, pr in prot:
            rule = pr["decision_rule"] or next(c["rule"] for c in led["checks"] if c["id"] == cid)
            lines.append(f"- **{cid}**: runs {pr['runs']} ({pr['runs_from']}); seeds {pr['seeds']}; rule: "
                         f"{_cell(rule, 220)}" + (f"; relation {pr['relation']}" if pr.get("relation") else "")
                         + (f"; pilot {pr['pilot_seconds']:.0f}s" if pr.get("pilot_seconds") else "")
                         + (f"; replicates extended {pr['replicates_extended']}" if pr.get("replicates_extended") else "")
                         + (f"; replicate extension refused: {pr['extension_refused']}" if pr.get("extension_refused") else ""))
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
        linked = ", ".join(f"{k['id']} {k['status']}" + (" (about a changed claim)" if k["status"] in FAILURE + SUPPORT and _moved(k)
                                                         else "") for k in led["checks"] if c["id"] in k["concerns"])
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
