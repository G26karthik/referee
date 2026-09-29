"""The review protocol: which task is owed next, and what a sealed answer must satisfy.

`advance` derives everything from files on disk (idempotent; safe to call any time) and
returns the tasks a worker can do now. `seal` validates one worker answer and stores only
harness-derived fields (invariant 2: a model's own status/verdict fields are never copied).
Phases: read (4 lenses) -> critic -> plan -> verify (per check) -> report -> done.
"""
from __future__ import annotations

import json
import re
import shutil
import time
from pathlib import Path

from . import execute, paper, reconcile, report, state
from .evidence import command, documented, flat, has_word, interval, relation, value_in
from .repo import listing, released

LENSES = ("overclaim", "protocol", "confound", "contradiction")
KINDS = ("AUTHOR_CODE", "RELEASED_DATA", "RECONSTRUCTION", "CERTIFICATE", "ARITHMETIC")
SCRIPT_KINDS = ("RELEASED_DATA", "RECONSTRUCTION", "CERTIFICATE")
REQUIRED = {"CERTIFICATE": ("hypotheses", "claimed_bound", "instance"),
            "RELEASED_DATA": ("dataset", "metric", "comparison_target"),
            "RECONSTRUCTION": ("method", "training", "dataset", "metric", "comparison_target")}
SEVERITY = ("NOTE", "MINOR", "MAJOR", "FATAL")
CLASSES = ("CONFIRMED_FINDING", "PLAUSIBLE_CONCERN", "OPEN_QUESTION", "DISMISSED")
PROMPTS = Path(__file__).parent / "prompts"
_EFFORT = {"lens": "high", "critic": "high", "plan": "high", "bind": "high", "gen": "high",
           "verify": "high", "report": "medium"}


class SealError(ValueError):
    """A fixable problem with a worker's answer; the worker is told exactly what."""


# --- files ------------------------------------------------------------------------------
def _safe(tid: str) -> str:
    return re.sub(r"[^\w.-]+", "__", tid)


def _sealed(root: Path, tid: str) -> dict | None:
    """A sealed record, only if its bytes still hash to what was sealed."""
    path = root / "sealed" / f"{_safe(tid)}.json"
    seals = state.read_json(root / "seals.json", {})
    if tid not in seals or not path.is_file() or state.sha256(path.read_bytes()) != seals[tid]:
        return None
    return state.read_json(path)


def _template(name: str, **kw) -> str:
    text = (PROMPTS / f"{name}.md").read_text(encoding="utf-8")
    for k, v in {"security": (PROMPTS / "security.md").read_text(encoding="utf-8"), **kw}.items():
        text = text.replace("{{" + k + "}}", str(v))
    return text


def _section(name: str, title: str) -> str:
    """The `## title` section of prompts/<name>.md."""
    text = (PROMPTS / f"{name}.md").read_text(encoding="utf-8")
    m = re.search(rf"(?ms)^## {re.escape(title)}\n(.*?)(?=^## |\Z)", text)
    return m.group(1).strip() if m else ""


def read_ranges(path: Path, chunk: int) -> list[list]:
    """[path, offset, limit] line ranges of about `chunk` chars, so a worker reads a file in
    one turn of parallel Reads, each under the Read tool's cap."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out, start, size = [], 0, 0
    for i, line in enumerate(lines):
        if size and size + len(line) + 1 > chunk:
            out.append([path.as_posix(), start + 1, i - start])
            start, size = i, 0
        size += len(line) + 1
    return out + ([[path.as_posix(), start + 1, len(lines) - start]] if lines[start:] else [])


# --- the protocol -----------------------------------------------------------------------
class _Ctx:
    def __init__(self, cfg: state.Config, pid: str):
        self.cfg, self.pid, self.root = cfg, pid, state.pdir(cfg, pid)
        self.meta, self.paper = paper.load(cfg, pid)
        self.src = state.read_json(self.root / "source.json", {})
        self.checkout = self.root / "repo"
        self.pages_dir = (self.root / "paper" / "pages").as_posix()

    def sealed(self, tid: str) -> dict | None:
        return _sealed(self.root, tid)

    def tracked(self) -> set[str]:
        if not hasattr(self, "_tracked"):
            from .repo import git
            rc, out = git(["ls-files", "-z"], self.checkout, 60) if (self.checkout / ".git").is_dir() else (1, "")
            self._tracked = set(out.split("\0")) - {""} if rc == 0 else set()
        return self._tracked

    def concerns(self) -> list[dict]:
        out = [c for lens in LENSES for c in (self.sealed(f"lens:{lens}") or {}).get("concerns", [])]
        crit = {r["id"]: r for r in (self.sealed("critic") or {}).get("reviews", [])}
        for c in out:
            r = crit.get(c["id"], {})
            if r.get("severity") in SEVERITY and SEVERITY.index(r["severity"]) < SEVERITY.index(c["severity"]):
                c["severity"], c["critic"] = r["severity"], r.get("reason", "")   # only ever lowered
            if r.get("withdraw"):
                c["withdrawn"] = r.get("reason", "") or "withdrawn by the critic"
        return out

    def task(self, tid: str, role: str, prompt: str, extra_reads: tuple[Path, ...] = (),
             paper: bool = True) -> dict:
        path = self.root / "tasks" / f"{_safe(tid)}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(prompt, encoding="utf-8")
        paper_md = (self.root / "paper" / "paper.md",) if paper else ()
        reads = [r for p in (path, *paper_md, *extra_reads) for r in read_ranges(p, self.cfg.read_chunk)]
        return {"id": tid, "role": role, "prompt": path.as_posix(),
                "out": (self.root / "out" / f"{_safe(tid)}.json").as_posix(),
                "effort": _EFFORT[role], "reads": reads, "after": []}


def _concern_lines(concerns: list[dict]) -> str:
    return "\n".join(json.dumps({k: c.get(k) for k in ("id", "lens", "severity", "class", "central", "checkable",
                                                         "statement", "evidence")}, ensure_ascii=False)
                     for c in concerns if not c.get("withdrawn")) or "(no concerns survived)"


def _repo_text(x: _Ctx) -> tuple[str, str]:
    if not (x.checkout / ".git").is_dir():
        return (f"none ({x.src.get('evidence') or 'the paper advertises no repository of its own'})",
                "(no checkout)")
    rel = state.read_json(x.root / "released.json")
    if rel is None:                    # hashed once, at the pinned commit
        rel = released(x.checkout)
        state.write_json(x.root / "released.json", rel)
    return (f"{x.src['url']} @ {x.src.get('commit', '')[:12]} (attributed by {x.src.get('discovered_by')}: "
            f"\"{x.src.get('evidence', '')[:300]}\")", listing(x.checkout, rel))


def _plan(x: _Ctx) -> tuple[str, list[dict], list[dict]]:
    """(phase, tasks, executions to start)."""
    missing = [lens for lens in LENSES if x.sealed(f"lens:{lens}") is None]
    title = x.meta["title"]
    if missing:
        return "read", [x.task(f"lens:{lens}", "lens", _template(
            "lens", lens=lens, focus=_section("lenses", lens), title=title, pages_dir=x.pages_dir))
            for lens in missing], []
    if x.sealed("critic") is None:
        return "critic", [x.task("critic", "critic", _template(
            "critic", title=title, pages_dir=x.pages_dir, concerns=_concern_lines(x.concerns())))], []
    plan = x.sealed("plan")
    rows = (x.root / "paper" / "rows.md",)
    if plan is None:
        repo_line, lst = _repo_text(x)
        return "plan", [x.task("plan", "plan", _template(
            "plan", title=title, pages_dir=x.pages_dir, repo=repo_line, checkout=x.checkout.as_posix(),
            gpu="unknown until first use", concerns=_concern_lines(x.concerns()), listing=lst,
            max_checks=x.cfg.max_checks), rows)], []
    tasks = []
    # Environments start as soon as an unfinished check needs them; each advance moves them one step.
    open_ = [c for c in plan["checks"] if not (x.root / "checks" / c["id"] / "outcome.json").exists()]
    if (x.checkout / ".git").is_dir() and plan["repo_is_authors"] and any(
            c["kind"] in ("AUTHOR_CODE", "RECONSTRUCTION") for c in open_):
        execute.author_env(x.cfg, x.root)
    if any(c["kind"] in SCRIPT_KINDS for c in open_):
        execute.ensure_env(x.cfg, x.root, x.cfg.projects / ".script-env", execute.DEFAULT_IMAGE, None,
                           execute.SCRIPT_PACKAGES)
    for c in plan["checks"]:
        tasks += _step(x, c)
    running = [c["id"] for c in plan["checks"] if execute.poll(x.cfg, x.pid, c["id"])]
    if tasks or running:
        return "verify", tasks, running
    ledger = report.ledger(x)
    if x.sealed("report") is None:
        return "report", [x.task("report", "report", _template(
            "report", title=title, table=report.table(ledger), concerns=_concern_lines(ledger["concerns"]),
            checks=json.dumps(ledger["checks"], ensure_ascii=False, indent=1)[:40_000],
            central=json.dumps(ledger["central_claims"], ensure_ascii=False, indent=1)),
            paper=False)], []   # the writer works from the ledger; its quotes are already in it
    report.render(x, ledger, x.sealed("report"))
    return "done", [], []


def _step(x: _Ctx, c: dict) -> list[dict]:
    """The tasks owed for one check now (starting its execution when it is ready)."""
    cid, kind = c["id"], c["kind"]
    cdir = x.root / "checks" / cid
    if (cdir / "outcome.json").exists() or (cdir / "exec.json").exists():
        return []
    spec = _spec_text(c)
    if kind == "AUTHOR_CODE":
        b = x.sealed(f"bind:{cid}")
        if b is None:
            _, lst = _repo_text(x)
            t = c["target"]
            return [x.task(f"bind:{cid}", "bind", _template(
                "bind", title=x.meta["title"], pages_dir=x.pages_dir, claim=c["claim"],
                target=json.dumps(t, ensure_ascii=False), page=t["page"],
                page_png=f"{x.pages_dir}/p{t['page']:03d}.png", checkout=x.checkout.as_posix(), listing=lst))]
        check = {**c, **b, "repo_attributed": c["repo_attributed"]}
        return _start(x, check)
    rounds = 1 + x.cfg.max_revisions
    if kind == "ARITHMETIC":
        v = x.sealed(f"verify:{cid}.1")
        if v is None:
            return [_verify_task(x, c, 1, spec, json.dumps(
                {k: c[k] for k in ("operands", "expression", "target")}, ensure_ascii=False, indent=1), "")]
        if v["verdict"] == "APPROVE":
            out = reconcile.arithmetic(*c["interval"], c["printed"])
        else:
            out = {"status": "NOT_CHECKABLE", "reason": f"verifier {v['verdict']}: {v['required_changes'] or v['notes']}"}
        state.write_json(cdir / "outcome.json", {"check": cid, "kind": kind, "evidence": "PAPER_ARITHMETIC",
                                                 "authorized": True, **out})
        return []
    for r in range(1, rounds + 1):
        g = x.sealed(f"gen:{cid}.{r}")
        if g is None:
            prev = x.sealed(f"verify:{cid}.{r - 1}") if r > 1 else None
            revision = "" if not prev else (
                f"\n=== REVISION {r}: an independent verifier rejected the previous attempt ===\nFix exactly "
                f"this, from the paper's own words (never by weakening the claim or inventing a detail):\n"
                f"{prev['required_changes'] or prev['notes']}\n--- the rejected script ---\n"
                f"{(cdir / f'script.{r - 1}.py').read_text(encoding='utf-8')}\n")
            try_cmd = (f'cd "{state.ROOT.as_posix()}" && PYTHONUTF8=1 SH_PROJECTS_DIR="{x.cfg.projects.as_posix()}" '
                       f'SH_ALLOW_INSTALL={int(x.cfg.allow_install)} SH_ALLOW_SCRIPT_EXEC={int(x.cfg.allow_script_exec)} '
                       f'"{Path(x.cfg.python).as_posix()}" run.py try {x.pid} gen:{cid}.{r}')
            extra = "" if kind not in ("RELEASED_DATA", "RECONSTRUCTION") else (
                "\n\n=== CHECKOUT (read-only; your working directory) ===\n" + _repo_text(x)[1])
            rel = (c.get("target") or {}).get("relation")
            metric = ("`violated`" if kind == "CERTIFICATE" else f"every output the relation `{rel}` names" if rel
                      else f"`{c['metric']}`" if c["metric"] else "the ONE output compared with the printed target; "
                      "name it in `metric`")
            env = ("the authors' environment (their lockfile/requirements, built from the checkout; if it fails to "
                   "build, the baseline below)" if kind == "RECONSTRUCTION" and c["repo_attributed"] else
                   "the baseline: Python 3.11, standard library plus numpy, scipy, pandas, scikit-learn, sympy")
            return [x.task(f"gen:{cid}.{r}", "gen", _template(
                "gen", title=x.meta["title"], pages_dir=x.pages_dir, check_id=cid, kind=kind, claim=c["claim"],
                spec=spec + extra, contract=_section("contracts", f"gen {kind}"), metric=metric, environment=env,
                required=", ".join(REQUIRED[kind]), max_tries=x.cfg.max_tries, try_cmd=try_cmd, revision=revision))]
        if g.get("refused"):
            return _terminal(cdir, c, "NOT_CHECKABLE", f"the script author refused: {g['notes'][:400]}")
        v = x.sealed(f"verify:{cid}.{r}")
        if v is None:
            tries = (cdir / "tries.jsonl").read_text(encoding="utf-8")[-6000:] if (cdir / "tries.jsonl").exists() else "(none)"
            proposal = json.dumps({"script": (cdir / f"script.{r}.py").read_text(encoding="utf-8"), **{
                k: g.get(k) for k in ("runs", "runs_quote", "metric", "outputs", "bindings", "deviations",
                                      "checked_statement")}}, ensure_ascii=False, indent=1)
            return [_verify_task(x, c, r, spec, proposal, tries)]
        if v["verdict"] == "APPROVE":
            check = {**c, "runs": g["runs"], "script_sha256": g["script_sha256"], "metric": g.get("metric", c["metric"]),
                     "deviations": g.get("deviations", []),
                     "approval": {"approved": True, "script_sha256": v["script_sha256"]}}
            shutil.copyfile(cdir / f"script.{r}.py", cdir / "script.py")
            return _start(x, check)
        if v["verdict"] == "UNCHECKABLE" or r == rounds:
            why = "unCheckable" if v["verdict"] == "UNCHECKABLE" else f"{rounds} rounds rejected"
            return _terminal(cdir, c, "NOT_CHECKABLE", f"verifier: {why}: {(v['required_changes'] or v['notes'])[:400]}")
    return []


def _terminal(cdir: Path, c: dict, status: str, reason: str) -> list:
    state.write_json(cdir / "outcome.json", {"check": c["id"], "kind": c["kind"], "status": status,
                                             "authorized": False, "reason": reason})
    return []


def _start(x: _Ctx, check: dict) -> list:
    """Record the check and hand it to the daemon-owned executor (the gate runs at its first poll)."""
    cdir = x.root / "checks" / check["id"]
    state.write_json(cdir / "check.json", check)
    if check["kind"] == "AUTHOR_CODE" and not check.get("identity", {}).get("established"):
        return _terminal(cdir, check, "BLOCKED", "not run: experiment identity is not established: "
                         + check.get("identity", {}).get("reason", ""))
    state.write_json(cdir / "exec.json", {"token": state.now()})
    return []


def _verify_task(x: _Ctx, c: dict, r: int, spec: str, proposal: str, tries: str) -> dict:
    return x.task(f"verify:{c['id']}.{r}", "verify", _template(
        "verify", title=x.meta["title"], pages_dir=x.pages_dir, check_id=c["id"], kind=c["kind"], claim=c["claim"],
        spec=spec, rules=_section("contracts", f"verify {c['kind']}"), proposal=proposal, tries=tries))


def _spec_text(c: dict) -> str:
    parts = []
    if c.get("target"):
        parts.append(f"Printed target: {json.dumps(c['target'], ensure_ascii=False)}")
    if c.get("statement"):
        parts.append(f"Statement (verbatim): {c['statement']}")
    if c.get("step"):
        parts.append(f"CHECK EXACTLY THIS PROOF STEP (verbatim; bound by the harness): {c['step']}")
    return "\n".join(parts)


def advance(cfg: state.Config, source: str, wait: int = 0) -> dict:
    """Advance one paper as far as the harness can alone; return what workers can do now."""
    pid = source if (state.pdir(cfg, source) / "paper" / "doc.json").exists() else None
    if pid is None:
        with state.lock(cfg.projects / ".ingest.lock"):
            pid = paper.ingest(cfg, Path(source))
    root = state.pdir(cfg, pid)
    deadline = time.time() + wait
    while True:
        with state.lock(root / ".lock"):
            x = _Ctx(cfg, pid)
            phase, tasks, running = _plan(x)
            for t in tasks:   # an answer left by an earlier attempt must never be sealed as this one's
                if (out := Path(t["out"])).exists():
                    out.replace(out.with_suffix(".stale.json"))
        if tasks or phase == "done" or time.time() >= deadline:
            break
        time.sleep(15)
    return {"paper_id": pid, "phase": phase, "status": "complete" if phase == "done" else "waiting",
            "blocked_reason": f"executions running: {', '.join(running)}" if running and not tasks else "",
            "scientific_status": report.scientific_status(root), "tasks": tasks}


# --- sealing ----------------------------------------------------------------------------
def seal(cfg: state.Config, pid: str, tid: str, path: str) -> dict:
    raw = Path(path).read_text(encoding="utf-8", errors="replace")
    body = raw[raw.find("{"):raw.rfind("}") + 1]
    try:
        obj = json.loads(body)
    except ValueError as e:
        raise SealError(f"the answer is not valid JSON: {e}") from e
    root = state.pdir(cfg, pid)
    with state.lock(root / ".lock"):
        x = _Ctx(cfg, pid)
        pending = {t["id"] for t in _plan(x)[1]}
        if tid not in pending:
            raise SealError(f"'{tid}' is not a pending task (pending: {sorted(pending)})")
        attempts = state.read_json(root / "attempts.json", {})
        attempts[tid] = attempts.get(tid, 0) + 1
        state.write_json(root / "attempts.json", attempts)
        role, final = tid.split(":")[0], attempts[tid] >= 3
        try:
            if not isinstance(obj, dict):
                raise TypeError("the answer must be a JSON object")
            rec = VALIDATORS[role](x, tid, obj, final=final)
        except SealError:
            raise
        except (AttributeError, TypeError, KeyError, ValueError, IndexError) as e:
            # A malformed answer is the worker's to fix; on the last attempt it seals as an
            # honest nothing, so one bad answer cannot stall the review.
            if not final:
                raise SealError(f"malformed answer ({type(e).__name__}: {e}); follow the JSON shape exactly") from e
            rec = {**_EMPTY[role], "malformed": f"{type(e).__name__}: {e}"[:300]}
        out = root / "sealed" / f"{_safe(tid)}.json"
        state.write_json(out, rec)
        seals = state.read_json(root / "seals.json", {})
        seals[tid] = state.sha256(out.read_bytes())
        state.write_json(root / "seals.json", seals)
        state.append_jsonl(root / "log.jsonl", {"event": "seal", "task": tid})
    return {"sealed": tid, "dropped": len(rec.get("dropped", []))}


def _fail_or_drop(errors: list[str], final: bool) -> None:
    """Fixable errors go back to the worker; on the final attempt the offending items are
    dropped (and recorded) instead, so one bad quote cannot stall a review."""
    if errors and not final:
        raise SealError("fix these (copy the paper's parsed text exactly; never weaken the claim):\n- "
                        + "\n- ".join(errors[:12]))


def _find(x: _Ctx, quote: str, errors: list[str], what: str) -> dict | None:
    hit, why = x.paper.find(quote or "")
    if hit is None:
        errors.append(f"{what}: {why}: {(quote or '')[:120]!r}")
    return hit


def _seal_lens(x: _Ctx, tid: str, obj: dict, final: bool) -> dict:
    lens, kept, dropped, errors = tid.split(":")[1], [], [], []
    for c in obj.get("concerns") or []:
        errs: list[str] = []
        ev = [(e, _find(x, e.get("quote", ""), errs, f"concern {c.get('title', '')[:40]!r}"))
              for e in (c.get("evidence") or [])]
        if not ev or errs:
            dropped.append({"title": c.get("title", ""), "why": errs or ["no evidence quote"]})
            errors += errs or [f"concern {c.get('title', '')[:40]!r} has no evidence quote"]
            continue
        sev = c.get("severity") if c.get("severity") in SEVERITY else "NOTE"
        cls = c.get("class") if c.get("class") in CLASSES else "OPEN_QUESTION"
        conf = c.get("confidence") if c.get("confidence") in ("HIGH", "MEDIUM", "LOW") else "LOW"
        # Evidence class bounds severity; nothing raises it (invariant 8).
        cap = {"DISMISSED": "NOTE", "OPEN_QUESTION": "MINOR", "PLAUSIBLE_CONCERN": "MAJOR"}.get(cls, "FATAL")
        cap = "MINOR" if conf == "LOW" else ("MAJOR" if cap == "FATAL" and conf != "HIGH" else cap)
        counted = SEVERITY[min(SEVERITY.index(sev), SEVERITY.index(cap))]
        kept.append({"id": f"{lens}-{len(kept) + 1:02d}", "lens": lens, "lens_severity": sev, "severity": counted,
                     "confidence": conf, "class": cls, "central": c.get("central") is True,
                     "evidence": [{"quote": h["quote"], "page": h["page"], "role": e.get("role", "")} for e, h in ev],
                     **{k: str(c.get(k) or "")[:2000] for k in ("title", "statement", "reasoning", "alternative",
                                                                 "why_alternative_fails", "steelman",
                                                                 "effect_on_claim", "checkable")},
                     "calculation": c.get("calculation") if isinstance(c.get("calculation"), dict) else None})
    _fail_or_drop(errors, final)
    return {"lens": lens, "concerns": kept, "dropped": dropped,
            "unasked_question": str(obj.get("unasked_question") or "")[:2000], "notes": str(obj.get("notes") or "")[:2000]}


def _seal_critic(x: _Ctx, tid: str, obj: dict, final: bool) -> dict:
    known = {c["id"] for c in x.concerns()}
    return {"reviews": [{"id": r["id"], "severity": r.get("severity"), "withdraw": r.get("withdraw") is True,
                         "reason": str(r.get("reason") or "")[:1000]}
                        for r in obj.get("reviews") or [] if isinstance(r, dict) and r.get("id") in known]}


def _target(x: _Ctx, t: dict, errors: list[str], cid: str) -> dict | None:
    if (t or {}).get("relation"):      # a stated comparison: the sentence re-found, the relation parsed
        try:
            names = relation(str(t["relation"]))[3]
        except ValueError as e:
            errors.append(f"{cid}: relation: {e}")
            return None
        hit = _find(x, t.get("quote", ""), errors, f"{cid} relation quote")
        return hit and {"quote": hit["quote"], "relation": str(t["relation"]), "names": names, "page": hit["page"]}
    value = str((t or {}).get("value") or "").strip()
    if not value:
        errors.append(f"{cid}: the target has no printed value")
        return None
    if t.get("row_quote"):
        hit, why = x.paper.cell(str(t["row_quote"]), value, str(t.get("column_quote") or ""),
                                t["page"] if isinstance(t.get("page"), int) else 0)
        if not hit:
            errors.append(f"{cid}: {why}")
            return None
        return {"row_quote": t["row_quote"], "column_quote": t["column_quote"], "value": value, **hit}
    hit = _find(x, t.get("quote", ""), errors, f"{cid} target")
    if hit and not value_in(hit["quote"], value):
        errors.append(f"{cid}: the value {value!r} is not printed in the target quote")
        return None
    return hit and {"quote": hit["quote"], "value": value, "page": hit["page"]}


def _seal_plan(x: _Ctx, tid: str, obj: dict, final: bool) -> dict:
    errors, checks, dropped = [], [], []
    concern_ids = {c["id"] for c in x.concerns()}
    attributed = (x.checkout / ".git").is_dir() and obj.get("repo_is_authors") is True
    proposed = [c for c in obj.get("checks") or [] if isinstance(c, dict)]
    # Central claims get the budget first: a check no central claim cites is incidental, is
    # cut before any central one, and must say why no central claim could use its slot.
    central_ids = {k for cc in obj.get("central_claims") or [] if isinstance(cc, dict) for k in cc.get("checks") or []}
    proposed.sort(key=lambda c: c.get("id") not in central_ids)
    for c in proposed[x.cfg.max_checks:]:
        dropped.append({"check": c.get("id"), "why": f"over the budget of {x.cfg.max_checks} checks"})
    for i, c in enumerate(proposed[:x.cfg.max_checks], 1):
        cid, kind, errs = f"C{i}", c.get("kind"), []
        if kind not in KINDS:
            errs.append(f"{cid}: unknown kind {kind!r}")
        claim = _find(x, c.get("claim_quote", ""), errs, f"{cid} claim_quote")
        rec = {"id": cid, "proposed_id": c.get("id"), "kind": kind, "concerns": [k for k in c.get("concerns") or []
                                                                               if k in concern_ids],
               "claim": claim["quote"] if claim else "", "why": str(c.get("why") or "")[:1000],
               "metric": str(c.get("metric") or "").strip(), "repo_attributed": attributed,
               "central": c.get("id") in central_ids, "incidental_why": str(c.get("incidental_why") or "")[:600]}
        if not rec["central"] and not rec["incidental_why"]:
            errs.append(f"{cid}: no central claim lists this check: link it from `central_claims[].checks`, or give "
                        "`incidental_why` (why no central claim could use this check slot)")
        if kind != "CERTIFICATE":
            rec["target"] = _target(x, c.get("target") or {}, errs, cid)
            rec["printed"] = (rec["target"] or {}).get("value", "")
            if (rec["target"] or {}).get("relation") and kind not in ("RELEASED_DATA", "RECONSTRUCTION"):
                errs.append(f"{cid}: a relation target is checked by RELEASED_DATA or RECONSTRUCTION only")
        if kind == "AUTHOR_CODE" and not rec["metric"]:   # a script's compared output is named by its author
            errs.append(f"{cid}: `metric` must name the program output compared with the printed target")
        if kind == "AUTHOR_CODE":
            if not attributed:
                errs.append(f"{cid}: AUTHOR_CODE needs a checkout you judge to be the authors' own")
            elif not documented(_repo_file(x, c.get("command_file")), str(c.get("command_quote") or "")):
                errs.append(f"{cid}: command_quote is not a whole documented command in tracked file "
                            f"{c.get('command_file')!r}")
            prep = str(c.get("prepare_quote") or "")
            if prep and not documented(_repo_file(x, c.get("prepare_file")), prep):
                errs.append(f"{cid}: prepare_quote is not a whole documented command in {c.get('prepare_file')!r}")
            rec.update(command_quote=command(str(c.get("command_quote") or "")), prepare_quote=command(prep))
        if kind == "CERTIFICATE":
            st = _find(x, c.get("statement_quote", ""), errs, f"{cid} statement_quote")
            sp = _find(x, c["step_quote"], errs, f"{cid} step_quote") if c.get("step_quote") else None
            rec.update(statement=st["quote"] if st else "", step=sp["quote"] if sp else "")
        if kind == "ARITHMETIC":
            printed, ops = {}, []
            for o in [o for o in c.get("operands") or [] if isinstance(o, dict)]:
                h = _find(x, str(o.get("quote") or ""), errs, f"{cid} operand {o.get('name')}")
                name, val = str(o.get("name") or ""), str(o.get("value") or "")
                if h and not (name.isidentifier() and value_in(h["quote"], val)):
                    errs.append(f"{cid}: operand {name!r} value {val!r} is not printed in its quote")
                elif h:
                    printed[name] = val
                    ops.append({"name": name, "quote": h["quote"], "value": val})
            try:
                rec["interval"] = list(interval(str(c.get("expression") or ""), printed)) if not errs else None
            except ValueError as e:
                errs.append(f"{cid}: expression: {e}")
            rec.update(operands=ops, expression=str(c.get("expression") or ""))
        if errs:
            errors += errs
            dropped.append({"check": cid, "why": errs})
        else:
            checks.append(rec)
    central = []
    for cc in [cc for cc in obj.get("central_claims") or [] if isinstance(cc, dict)]:
        h = _find(x, str(cc.get("quote") or ""), errors, "central claim")
        if not h:
            dropped.append({"check": "central claim", "why": f"quote not found: {str(cc.get('quote'))[:120]!r}"})
        else:
            central.append({"quote": h["quote"], "page": h["page"],
                            "checks": [k for k in cc.get("checks") or [] if k in {c.get("id") for c in proposed}],
                            "why_unchecked": str(cc.get("why_unchecked") or "")[:600]})
    _fail_or_drop(errors, final)
    # The planner's own ids map onto the harness's C1..Cn.
    ids = {c["proposed_id"]: c["id"] for c in checks}
    for cc in central:
        cc["checks"] = [ids[k] for k in cc["checks"] if k in ids]
    return {"checks": checks, "central_claims": central, "dropped": dropped,
            "repo_is_authors": attributed, "repo_note": str(obj.get("repo_note") or "")[:1000]}


def _repo_file(x: _Ctx, rel) -> Path | None:
    """A file git tracks in the pinned checkout — never a path outside it."""
    rel = str(rel or "").strip().replace("\\", "/").removeprefix("./")
    return x.checkout / rel if rel and rel in x.tracked() else None


_PLACEHOLDER = re.compile(r"<[^<>]+>|\$\{?\w|\{\w*\}|\[[A-Z][A-Z_ -]*\]|path/to|/path/|\.\.\.|YOUR_", re.I)


def _runs(x: _Ctx, runs, quote, why: list[str]) -> int | None:
    """A paper-stated run count, only if its sentence is re-found printing that number."""
    if not isinstance(runs, int) or isinstance(runs, bool) or runs < 1:
        return None
    if runs > 1 and not ((h := x.paper.find(str(quote or ""))[0]) and value_in(h["quote"], str(runs))):
        return None
    if runs > x.cfg.max_runs:
        why.append(f"the paper's {runs} runs exceed SH_MAX_RUNS={x.cfg.max_runs}: refused rather than downscaled")
    return runs


def _seal_bind(x: _Ctx, tid: str, obj: dict, final: bool) -> dict:
    """Two-key identity: established only if this independent answer names the same whole
    documented command (and prepare step) and metric key as the planner, each at the pinned
    commit: the command as a full line or code span of a tracked doc, the key as a whole word
    of a tracked code file."""
    cid = tid.split(":")[1]
    c = next(k for k in x.sealed("plan")["checks"] if k["id"] == cid)
    cmd, why, errors = command(str(obj.get("command_quote") or "")), [], []
    if cmd and not documented(_repo_file(x, obj.get("command_file")), cmd):
        errors.append(f"command_quote is not a whole documented command in tracked file {obj.get('command_file')!r}")
    _fail_or_drop(errors, final)
    key, flag = str(obj.get("metric_key") or "").strip(), str(obj.get("seed_flag") or "").strip()
    if not cmd:
        why.append(f"no documented command produces this number: {str(obj.get('notes') or '')[:300]}")
    why += errors
    if cmd and _PLACEHOLDER.search(cmd):
        why.append("the documented command has unfilled placeholders")
    if cmd and cmd != c["command_quote"]:
        why.append("the independent verifier named a different command than the planner")
    if key != c["metric"]:
        why.append(f"the independent verifier's metric key {key!r} differs from the planner's {c['metric']!r}")
    elif not has_word(_repo_file(x, obj.get("metric_file")), key):
        why.append(f"the metric key {key!r} is not a whole word of tracked code file {obj.get('metric_file')!r}")
    seeded = bool(re.fullmatch(r"--?[\w-]*seed[\w-]*", flag)) and bool(
        re.search(rf"(?<!\S){re.escape(flag)}(?:\s+|=)\d+(?!\S)", cmd))
    runs = _runs(x, obj.get("runs"), obj.get("runs_quote"), why) if seeded else 1
    prepare = command(str(obj.get("prepare_quote") or ""))
    if prepare != c.get("prepare_quote", "") or (prepare and not documented(_repo_file(x, obj.get("prepare_file")), prepare)):
        prepare = ""        # a prepare step both keys name, verbatim, or none (the run may then fail: INCONCLUSIVE)
    return {"identity": {"established": not why, "reason": "; ".join(why) or "two independent keys agree",
                         "basis": "independent verifier + whole documented command and metric key at the pinned commit"},
            "command": cmd, "metric": key or c["metric"], "seed_flag": flag if seeded else "",
            "runs": runs or 3, "runs_quote": str(obj.get("runs_quote") or "")[:500],
            "prepare": prepare, "notes": str(obj.get("notes") or "")[:1000]}


def _seal_gen(x: _Ctx, tid: str, obj: dict, final: bool) -> dict:
    cid, r = tid.split(":")[1].rsplit(".", 1)
    c = next(k for k in x.sealed("plan")["checks"] if k["id"] == cid)
    script = str(obj.get("script") or "")
    need = REQUIRED[c["kind"]]
    binds = {b.get("kind"): b for b in obj.get("bindings") or [] if isinstance(b, dict)}
    if not script.strip() or any(not (binds.get(k) or {}).get("impl_quote") for k in need):
        return {"refused": True, "notes": str(obj.get("notes") or "the script or a required binding is missing")[:2000]}
    errors, out = [], []
    rel = [f["path"] for f in state.read_json(x.root / "released.json", [])]
    for k in need:
        b = binds[k]
        if flat(b["impl_quote"]) not in flat(script):
            errors.append(f"binding {k}: impl_quote is not in the script")
        h = None if k == "instance" else _find(x, b.get("paper_quote", ""), errors, f"binding {k} paper_quote")
        out.append({"kind": k, "impl_quote": b["impl_quote"][:1000], "paper_quote": h["quote"] if h else "",
                    "page": h["page"] if h else None})
    if c["kind"] == "RELEASED_DATA" and not any(p in binds["dataset"]["impl_quote"] and p in script for p in rel):
        errors.append("the dataset binding must open a released file by its full relative path")
    if c["kind"] == "CERTIFICATE" and c.get("step") and flat(c["step"]) not in flat(binds["claimed_bound"].get("paper_quote", "")):
        errors.append("claimed_bound must be the proof step the harness bound")
    runs = obj.get("runs") if isinstance(obj.get("runs"), int) and not isinstance(obj.get("runs"), bool) else 1
    cap = 200 if c["kind"] == "CERTIFICATE" else x.cfg.max_runs     # ponytail: instances are cheap
    if not 1 <= runs <= cap:
        return {"refused": True, "notes": f"{runs} runs is outside 1..{cap}: refused rather than downscaled"}
    if runs > 1 and c["kind"] == "RECONSTRUCTION" and not (
            (h := x.paper.find(str(obj.get("runs_quote") or ""))[0]) and value_in(h["quote"], str(runs))):
        errors.append("runs > 1 needs runs_quote: the paper's sentence printing that number, verbatim")
    # The value compared is bound by NAME here, once: the relation's outputs, else the one
    # compared output (the planner's, or else the script author's), never a flag.
    outputs = [str(o) for o in obj.get("outputs") or []]
    rel = (c.get("target") or {}).get("relation")
    metric = "violated" if c["kind"] == "CERTIFICATE" else "" if rel else (c["metric"] or str(obj.get("metric") or "").strip())
    if rel and (miss := [n for n in c["target"]["names"] if n not in outputs]):
        errors.append(f"outputs must include every output the relation {rel!r} names; missing {miss}")
    elif c["kind"] != "CERTIFICATE" and not rel and (metric in ("", "violated") or metric not in outputs):
        errors.append("`metric` must name the ONE computed output compared with the printed target, and `outputs` "
                      "must include it (a flag computed against the printed number settles nothing)")
    deviations = []
    for d in [d for d in obj.get("deviations") or [] if isinstance(d, dict)][:8]:   # ponytail: 8 per script
        h = _find(x, str(d["printed"]), errors, "deviation `printed`") if d.get("printed") else None
        if not str(d.get("used") or "").strip():
            errors.append("each deviation needs `used`: what the script does instead of the printed text")
        deviations.append({"printed": h["quote"] if h else "", "page": h["page"] if h else None,
                           "used": str(d.get("used") or "")[:600], "why": str(d.get("why") or "")[:600]})
    if errors and final:
        return {"refused": True, "notes": "; ".join(errors)[:2000]}
    _fail_or_drop(errors, final)
    cdir = x.root / "checks" / cid
    cdir.mkdir(parents=True, exist_ok=True)
    (cdir / f"script.{r}.py").write_bytes(script.encode("utf-8"))   # bytes: the sha is of exactly these
    return {"script_sha256": state.sha256(script), "runs": runs, "runs_quote": str(obj.get("runs_quote") or "")[:500],
            "metric": metric, "outputs": outputs, "deviations": deviations, "bindings": out,
            "checked_statement": "proof_step" if c.get("step") else str(obj.get("checked_statement") or "conclusion"),
            "notes": str(obj.get("notes") or "")[:2000]}


def _seal_verify(x: _Ctx, tid: str, obj: dict, final: bool) -> dict:
    cid, r = tid.split(":")[1].rsplit(".", 1)
    verdict = obj.get("verdict") if obj.get("verdict") in ("APPROVE", "REVISE", "UNCHECKABLE") else None
    if verdict is None:
        raise SealError("verdict must be APPROVE, REVISE or UNCHECKABLE")
    errors: list[str] = []
    quotes = [h["quote"] for q in obj.get("quotes") or [] if (h := _find(x, str(q), errors, "verifier quote"))]
    if verdict == "APPROVE" and not quotes:
        _fail_or_drop(errors or ["APPROVE must cite the paper text relied on, verbatim"], final)
        verdict = "REVISE"          # an approval that cites nothing checkable is not an approval
    g = x.sealed(f"gen:{cid}.{r}") or {}
    return {"verdict": verdict, "required_changes": str(obj.get("required_changes") or "")[:3000],
            "notes": str(obj.get("notes") or "")[:2000], "quotes": quotes, "script_sha256": g.get("script_sha256", "")}


def _seal_report(x: _Ctx, tid: str, obj: dict, final: bool) -> dict:
    return {"summary_md": str(obj.get("summary_md") or "")[:8000]}


VALIDATORS = {"lens": _seal_lens, "critic": _seal_critic, "plan": _seal_plan, "bind": _seal_bind,
              "gen": _seal_gen, "verify": _seal_verify, "report": _seal_report}
_EMPTY = {"lens": {"concerns": [], "dropped": []}, "critic": {"reviews": []},
          "plan": {"checks": [], "central_claims": [], "dropped": [], "repo_is_authors": False, "repo_note": ""},
          "bind": {"identity": {"established": False, "reason": "malformed binding answer"}, "command": "",
                   "metric": "", "seed_flag": "", "runs": 1, "prepare": ""},
          "gen": {"refused": True, "notes": "malformed answer"},
          "verify": {"verdict": "UNCHECKABLE", "required_changes": "", "notes": "malformed verifier answer",
                     "quotes": [], "script_sha256": ""},
          "report": {"summary_md": ""}}


def try_(cfg: state.Config, pid: str, tid: str, script_path: str) -> dict:
    """A generator's draft run, counted against its budget and shown to its verifier."""
    root = state.pdir(cfg, pid)
    if not tid.startswith("gen:") or not (root / "tasks" / f"{_safe(tid)}.md").exists() \
            or _sealed(root, tid) is not None:
        return {"error": f"'{tid}' is not an open script-writing task"}
    cid = tid.split(":")[1].rsplit(".", 1)[0]
    cdir = state.pdir(cfg, pid) / "checks" / cid
    n = len((cdir / "tries.jsonl").read_text(encoding="utf-8").splitlines()) if (cdir / "tries.jsonl").exists() else 0
    if n >= cfg.max_tries * (1 + cfg.max_revisions):
        return {"error": f"the draft budget ({cfg.max_tries} per round) is spent"}
    script = Path(script_path).read_text(encoding="utf-8")
    res = execute.try_script(cfg, pid, cid, script)
    state.append_jsonl(cdir / "tries.jsonl", {"task": tid, "script_sha256": state.sha256(script), **res})
    return res
