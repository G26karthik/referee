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

from . import discover, execute, independence, paper, reconcile, report, state
from .evidence import Paper, clean_source, command, documented, flat, has_word, interval, mask, printed_form, relation, value_in
from .repo import listing, released

LENSES = ("overclaim", "protocol", "confound", "contradiction")
KINDS = ("AUTHOR_CODE", "RELEASED_DATA", "RECONSTRUCTION", "CERTIFICATE", "ARITHMETIC")
SCRIPT_KINDS = ("RELEASED_DATA", "RECONSTRUCTION", "CERTIFICATE")
REQUIRED = {"CERTIFICATE": ("hypotheses", "claimed_bound", "instance"),
            "RELEASED_DATA": ("dataset", "metric", "comparison_target"),
            "RECONSTRUCTION": ("method", "training", "dataset", "metric", "comparison_target")}
# What a check's evidence rests on: an audit of the authors' released result files, a recomputation
# of the metric from released per-item predictions or scores, a fresh run (training, simulation),
# exact instances, or the paper's own printed numbers.
BASIS = {"AUTHOR_CODE": "fresh_run", "RECONSTRUCTION": "fresh_run", "CERTIFICATE": "exact_instances",
         "ARITHMETIC": "paper_numbers"}
# An ingredient an experiment may genuinely lack: a simulation, a sequential test or an exact computation trains
# nothing. It is then declared `not_applicable` with its reason (the verifier checks it), never faked with a quote
# and never a reason to refuse an experiment whose other ingredients are all bound.
OPTIONAL = {"RECONSTRUCTION": ("training",)}
RELEASED_BASES = ("published_results", "predictions")
# A central claim's type decides which test may speak for it: an engineering claim (a component
# integrates, runs, trains) by a compatibility test; a comparison by a performance test.
CLAIM_TYPES = ("engineering", "performance", "value", "theory")
# An empirical claim (a comparison, a printed number, a component that trains) is satisfied only by an
# experiment on what it names; a mathematical check or a simulation beside it is supporting evidence.
EMPIRICAL = report.EMPIRICAL
ROLES = ("target", "supporting")
# Why a requested experiment or dataset is not run, each checked against what the harness itself holds:
# `data` against its registry searches (or a check that failed to acquire it), `credentials` against the
# registry record of the artifact (gated, private) unless it is a hosted closed service, `compute` against
# a measured run, the registry's size of the artifact and the measured host, or the paper's own statement of
# its compute. `protocol` and `other` are the planner's word, reported as such.
# `cap` is a configured cap of this run (a time budget, the search or check budget, the data cap) — a fact the harness
# holds, never a property of the experiment; it rests on the harness's own record of the cap being reached.
BLOCKERS = ("data", "credentials", "compute", "cap", "protocol", "other")
CRITERIA = ("stated", "supplied")
# ponytail: 16 deviations per script; more is several choices of one kind, merged into one entry. Past the cap
# a script is refused, never cut: a verifier must see every departure it judges.
MAX_DEVIATIONS = 16
# ponytail: 8 central claims per plan round cover a paper's abstract, contributions and conclusion; more is refused, and
# on the last attempt each claim past the cap is recorded as dropped, never cut unseen.
MAX_CLAIMS = 8
SEVERITY = ("NOTE", "MINOR", "MAJOR", "FATAL")
CLASSES = ("CONFIRMED_FINDING", "PLAUSIBLE_CONCERN", "OPEN_QUESTION", "DISMISSED")
PROMPTS = Path(__file__).parent / "prompts"
_EFFORT = {"lens": "high", "critic": "high", "claims": "high", "plan": "high", "bind": "high", "gen": "high", "vision": "medium",
           "verify": "high", "report": "medium", "audit": "high", "compare": "medium"}


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
    except (OSError, UnicodeDecodeError):
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
        self.waiting: set[str] = set()      # checks waiting on the harness (data, environment, smoke run)

    def sealed(self, tid: str) -> dict | None:
        return _sealed(self.root, tid)

    def plan(self) -> dict | None:
        """The plan with its follow-up round merged in (checks C1..Cn, then the follow-up's), and the claims of any
        follow-up plan the operator withdrew."""
        return report.merged(self.sealed("plan"), self.sealed("plan:2"), report.withdrawn_plans(self.root),
                             (self.sealed("claims") or {}).get("claims") or [])

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
            if isinstance(c.get("calculation"), dict) and c["class"] == "CONFIRMED_FINDING":
                # An arithmetic error read from extracted text stands only once the page image agrees.
                chk = _image_check(self, "vision:concerns", _calc_items(self, c["id"], c["calculation"]))
                if chk and chk["agrees"]:
                    c["image_check"] = "every number was read off the page image and agrees with the extracted text"
                elif chk:
                    c["class"], c["image_check"] = "OPEN_QUESTION", "the page image disagrees: " + chk["disagree"]
                    c["severity"] = SEVERITY[min(SEVERITY.index(c["severity"]), SEVERITY.index("MINOR"))]
                else:
                    c["class"], c["image_check"] = "PLAUSIBLE_CONCERN", "not yet read off the page image"
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


def _calc_items(x: "_Ctx", prefix: str, calc: dict) -> list[dict]:
    """The printed numbers an arithmetic claim rests on, each masked in its re-found context."""
    items = []
    parts = [(o.get("name"), o) for o in calc.get("operands") or [] if isinstance(o, dict)]
    if isinstance(calc.get("paper_result"), dict):
        parts.append(("result", calc["paper_result"]))
    elif isinstance(calc.get("target"), dict):
        parts.append(("result", calc["target"]))
    for name, o in parts:
        hit = x.paper.find(str(o.get("quote") or ""))[0]
        ctx = x.paper.masked_context(hit, str(o.get("value") or "")) if hit else None
        items.append({"id": f"{prefix}:{name}", "page": hit and hit["page"], "context": ctx,
                      "extracted": str(o.get("value") or "")})
    return items


def _image_check(x: "_Ctx", tid: str, items: list[dict]) -> dict | None:
    """The transcriber's reading of each item against the extraction; None until it is sealed.
    An item that cannot be located or masked cannot be confirmed."""
    read = (x.sealed(tid) or {}).get("read") if items else None
    if read is None:
        return None
    bad = [f"{i['id']}: image {read.get(i['id'], '')!r} vs extracted {i['extracted']!r}" for i in items
           if not i["context"] or printed_form(read.get(i["id"], "")) != printed_form(i["extracted"])]
    return {"agrees": not bad, "disagree": "; ".join(bad)[:600], "read": {i["id"]: read.get(i["id"]) for i in items}}


def _vision_task(x: "_Ctx", tid: str, items: list[dict]) -> list[dict]:
    todo = [i for i in items if i["context"]]
    if not todo or x.sealed(tid) is not None:
        return []
    lines = "\n".join(f"- id: {i['id']} | page image: {x.pages_dir}/p{i['page']:03d}.png | context: {i['context']!r}"
                      for i in todo)
    return [x.task(tid, "vision", _template("vision", title=x.meta["title"], pages_dir=x.pages_dir, items=lines),
                   paper=False)]


def _concern_vision(x: "_Ctx") -> list[dict]:
    raw = [c for lens in LENSES for c in (x.sealed(f"lens:{lens}") or {}).get("concerns", [])
           if isinstance(c.get("calculation"), dict) and c.get("class") == "CONFIRMED_FINDING"]
    return _vision_task(x, "vision:concerns", [i for c in raw for i in _calc_items(x, c["id"], c["calculation"])])


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
    # The main claims are extracted from the paper alone, beside the lenses and before any plan: a planner tests claims,
    # it never makes them (a criterion it supplies is a deviation, never a claim of the paper).
    claims = [] if x.sealed("claims") is not None or x.sealed("plan") is not None else [x.task(
        "claims", "claims", _template("claims", title=title, pages_dir=x.pages_dir))]
    if missing:
        return "read", [x.task(f"lens:{lens}", "lens", _template(
            "lens", lens=lens, focus=_section("lenses", lens), title=title, pages_dir=x.pages_dir))
            for lens in missing] + claims, []
    if x.sealed("critic") is None:
        return "critic", [x.task("critic", "critic", _template(
            "critic", title=title, pages_dir=x.pages_dir, concerns=_concern_lines(x.concerns())))] + claims, []
    if claims:
        return "claims", claims + _concern_vision(x), []
    plan = x.plan()
    rows = (x.root / "paper" / "rows.md",)
    if plan is None:
        return "plan", [_plan_task(x, "plan", "")] + _concern_vision(x), []
    tasks = _concern_vision(x)
    # Environments and cited data start as soon as an unfinished check needs them; each advance
    # moves them one step.
    open_ = [c for c in plan["checks"] if not (x.root / "checks" / c["id"] / "outcome.json").exists()]
    if (x.checkout / ".git").is_dir() and plan["repo_is_authors"] and any(
            c["kind"] in ("AUTHOR_CODE", "RECONSTRUCTION") for c in open_):
        execute.author_env(x.cfg, x.root)
    if any(c["kind"] in SCRIPT_KINDS for c in open_):
        execute.ensure_env(x.cfg, x.root, x.cfg.projects / ".script-env", execute.DEFAULT_IMAGE, None,
                           execute.SCRIPT_PACKAGES)
    for c in open_:
        if c.get("acquire") and execute.fetch(x.cfg, x.root, c["id"], c["acquire"]) is None:
            x.waiting.add(c["id"])
    for c in plan["checks"]:
        tasks += _step(x, c)
    tasks += _audits(x, plan)
    executing = [c["id"] for c in plan["checks"] if execute.poll(x.cfg, x.pid, c["id"])]
    running = executing + sorted(x.waiting)
    # The follow-up round does not wait on a long run: once only executions remain and each has at least
    # FOLLOWUP_AFTER_S left (measured from its pilot), the follow-up plans for what has ended, beside them; what they
    # find is reported when they end. A costly training run never holds back a proof check or a cheap experiment.
    # Once offered, the follow-up stays pending until it is sealed (its planner may take longer than a run has left).
    offered = (x.cfg.max_followup_checks > 0 and x.sealed("plan:2") is None
               and (x.root / "tasks" / f"{_safe('plan:2')}.md").exists())
    early = offered or (executing and not tasks and not x.waiting and x.cfg.max_followup_checks > 0
                        and x.sealed("plan:2") is None
                        and all((execute.remaining_s(x.cfg, x.root, k) or 0) >= FOLLOWUP_AFTER_S for k in executing))
    if (tasks or running) and not early:
        return "verify", tasks, running
    ledger = report.ledger(x)
    # Coverage: once every check ended, one follow-up plan sees what each check found and why; it
    # may add checks for central claims still undecided, and for any headline claim the first plan
    # never listed (or say why none can decide them).
    # ...and an empirical claim whose requested experiment did not run as specified (blocked, partial, or
    # run only on a substitute) is undecided however its supporting checks came out.
    ran = {r["claim"]: r["experiment"] for r in ledger["completion"]["claims"]}
    undecided = [cc for cc in ledger["central_claims"] if not set(cc["checks"]) & set(executing) and (
                 cc["claim_status"] in ("NOT_CHECKED", "NOTHING_DECIDED", "PARTIAL_EVIDENCE")
                 or (cc.get("claim_type") != "theory" and ran.get(cc["quote"]) not in (None, "RAN_AS_SPECIFIED")))]
    if x.cfg.max_followup_checks > 0 and x.sealed("plan:2") is None:
        return "plan", tasks + [_plan_task(x, "plan:2", _followup_text(ledger, undecided, executing))], running
    if running:
        return "verify", [], running
    if x.sealed("report") is None:
        from . import reviewer
        draft = reviewer.render(x, ledger, None)          # the harness's own page, before any model text
        return "report", [x.task("report", "report", _template(
            "report", title=title, draft=draft[:30_000],
            checks=json.dumps([_brief(c) for c in ledger["checks"]], ensure_ascii=False, indent=1)[:40_000],
            central=json.dumps([{k: cc.get(k) for k in ("id", "statement", "quote", "claim_type", "scope", "decision",
                                                        "completion", "checks", "why_unchecked")}
                                for cc in ledger["central_claims"]], ensure_ascii=False, indent=1)[:40_000]))], []
    # Another reproduction record (an HF logbook) is compared only once the decisions are sealed: it is shown beside each
    # claim with whether the tests are comparable, and never changes a decision.
    if references(x) and x.sealed("compare") is None:
        return "compare", [_compare_task(x, ledger)], []
    report.render(x, ledger, x.sealed("report"))
    from . import reviewer
    reviewer.render(x, ledger, x.sealed("report"), x.sealed("compare"))
    return "done", [], []


def _brief(c: dict) -> dict:
    """What a report writer needs of one check (statuses, numbers, deviations), without the raw value lists."""
    return {k: c.get(k) for k in ("id", "kind", "role", "claim", "covers", "status", "state", "reason", "rule", "basis",
                                  "test", "target", "stages", "outputs", "counts", "readings", "fidelity", "deviations",
                                  "data_identity", "literal", "admissible", "status_on_completed", "data_changed", "audit")
            if c.get(k) not in (None, "", [], {})}


def references(x: _Ctx) -> list[dict]:
    """The other reproduction records the operator registered for this paper (run.py reference), each still hashing to
    what was registered."""
    reg = state.read_json(x.root / "reference" / "index.json", []) or []
    return [r for r in reg if (x.root / "reference" / r["file"]).is_file()
            and state.sha256((x.root / "reference" / r["file"]).read_bytes()) == r["sha256"]]


def register_reference(cfg: state.Config, pid: str, path: str, source: str) -> dict:
    """Register another reproduction record (an HF logbook or verdict file) for a paper whose review is sealed: it is
    copied into the project and hashed, and a comparison task is owed. Refused before the report is sealed, so it can
    never inform a decision."""
    root = state.pdir(cfg, pid)
    with state.lock(root / ".lock"):
        if _sealed(root, "report") is None:
            return {"error": "the review is not sealed yet: a reference record is compared only after REFEREE's own decisions"}
        src = Path(path)
        data = src.read_bytes()
        try:
            data.decode("utf-8")
        except UnicodeDecodeError:
            return {"error": f"{src.name} is not UTF-8 text: register the record's text (markdown, JSON, plain text)"}
        reg = state.read_json(root / "reference" / "index.json", []) or []
        name = f"{len(reg) + 1}_{re.sub(r'[^A-Za-z0-9_.-]+', '_', src.name)[:80]}"
        (root / "reference").mkdir(parents=True, exist_ok=True)
        (root / "reference" / name).write_bytes(data)
        reg.append({"file": name, "source": source[:80], "sha256": state.sha256(data), "registered_at": state.now(),
                    "original": str(src)[:300]})
        state.write_json(root / "reference" / "index.json", reg)
        seals = state.read_json(root / "seals.json", {}) or {}
        seals.pop("compare", None)                # a new record: the comparison is made again over all of them
        state.write_json(root / "seals.json", seals)   # (and the review reads IN PROGRESS until it is rendered again)
        state.append_jsonl(root / "log.jsonl", {"event": "reference", "file": name, "source": source[:80]})
        return {"registered": name, "sha256": state.sha256(data)}


def _compare_task(x: _Ctx, ledger: dict) -> dict:
    refs = references(x)
    return x.task("compare", "compare", _template(
        "compare", title=x.meta["title"],
        refs="\n".join(f"- {(x.root / 'reference' / r['file']).as_posix()} (source: {r['source']})" for r in refs),
        central=json.dumps([{k: cc.get(k) for k in ("id", "statement", "quote", "claim_type", "scope", "decision", "checks")}
                            for cc in ledger["central_claims"]], ensure_ascii=False, indent=1)[:30_000],
        checks=json.dumps([_brief(c) for c in ledger["checks"]], ensure_ascii=False, indent=1)[:40_000]),
        tuple(x.root / "reference" / r["file"] for r in refs), paper=False)


def _seal_compare(x: _Ctx, tid: str, obj: dict, final: bool) -> dict:
    """One entry per main claim: what the other record found (its own words, verbatim from a registered file), whether
    the two tests are comparable (data, metric, settings, estimator) and why, and whether they agree. The record's
    verdict words are its own, quoted; nothing here moves a REFEREE decision."""
    refs = references(x)
    texts = {r["file"]: flat((x.root / "reference" / r["file"]).read_text(encoding="utf-8", errors="replace")) for r in refs}
    ids = {cc.get("id") for cc in (x.plan() or {}).get("central_claims", []) if cc.get("id")}
    errors, out = [], []
    for e in [e for e in obj.get("claims") or [] if isinstance(e, dict)][:40]:
        k = str(e.get("id") or "")
        if k not in ids:
            errors.append(f"claim id {k!r} is not one of {sorted(ids)}")
            continue
        quotes = [str(q) for q in e.get("quotes") or [] if str(q).strip()][:4]
        bad = [q[:80] for q in quotes if len(flat(q)) < 15 or not any(flat(q) in t for t in texts.values())]
        if bad:
            errors.append(f"{k}: quote(s) {bad} are not verbatim text (15+ chars) of a registered record")
            quotes = [q for q in quotes if q[:80] not in bad]
        comparable = _enum(e.get("comparable"), ("yes", "partly", "no"))
        agreement = _enum(e.get("agreement"), ("agrees", "disagrees", "not_comparable", "not_covered"))
        why = str(e.get("why") or "").strip()
        if not comparable or not agreement or len(why) < 20:
            errors.append(f"{k}: `comparable` (yes|partly|no), `agreement` (agrees|disagrees|not_comparable|not_covered) "
                          "and `why` (what differs: data, metric, settings, estimator, seeds) are required")
        if agreement in ("agrees", "disagrees") and not quotes:
            errors.append(f"{k}: an agreement or disagreement rests on the record's own words (`quotes`)")
            agreement = "not_comparable"
        out.append({"id": k, "source": str(e.get("source") or (refs[0]["source"] if refs else "reference"))[:80],
                    "reference_finding": str(e.get("reference_finding") or "")[:600], "quotes": quotes,
                    "comparable": comparable or "no", "agreement": agreement or "not_comparable", "why": why[:800]})
    _fail_or_drop(errors, final)
    return {"claims": out, "notes": str(obj.get("notes") or "")[:2000]}


def _discovery_text(x: _Ctx) -> str:
    done = discover.records(x.cfg, x.pid)
    return "\n".join((f"  {r['id']}: {r['query']!r} -> " + ", ".join(
        f"{k} {len(v['candidates'])}" + (f" (error: {v['error'][:60]})" if v["error"] else "") for k, v in r["results"].items()))
        if "results" in r else f"  {r['id']}: files of {r['files_of']} -> {len(r['files'])} file(s)" for r in done) or "  (none yet)"


def _plan_task(x: _Ctx, tid: str, followup: str) -> dict:
    repo_line, lst = _repo_text(x)
    find = (f'cd "{state.ROOT.as_posix()}" && PYTHONUTF8=1 SH_PROJECTS_DIR="{x.cfg.projects.as_posix()}" '
            f'SH_ALLOW_NETWORK={int(x.cfg.allow_network)} SH_ALLOW_DATA_SEARCH={int(x.cfg.allow_data_search)} '
            f'SH_MAX_DISCOVERIES={x.cfg.max_discoveries} SH_DENY_SOURCES="{",".join(x.cfg.deny_sources)}" '
            f'"{Path(x.cfg.python).as_posix()}" run.py discover {x.pid}')
    extracted = bool((x.sealed("claims") or {}).get("claims"))
    return x.task(tid, "plan", _template(
        "plan", title=x.meta["title"], pages_dir=x.pages_dir, repo=repo_line, checkout=x.checkout.as_posix(),
        claims=_claims_text(x), claims_step=_section("plan_steps", "extracted" if extracted else "legacy"),
        host=execute.host_facts(x.cfg), concerns=_concern_lines(x.concerns()), listing=lst,
        max_checks=x.cfg.max_followup_checks if followup else x.cfg.max_checks, followup=followup,
        discover_cmd=find, discover_files_cmd=f"{find} --files", discoveries=_discovery_text(x), max_discoveries=x.cfg.max_discoveries,
        data_search="on" if x.cfg.allow_network and x.cfg.allow_data_search else "OFF (SH_ALLOW_DATA_SEARCH / SH_ALLOW_NETWORK)"),
        (x.root / "paper" / "rows.md",))


# ponytail: a follow-up round waits for a run with less than 20 min left (what it finds informs the follow-up); a longer
# one runs beside the follow-up, and what it finds is reported when it ends.
FOLLOWUP_AFTER_S = 1200


def _followup_text(ledger: dict, undecided: list[dict], executing: list[str] = ()) -> str:
    done = [{k: c.get(k) for k in ("id", "kind", "claim", "role", "covers", "status", "state", "reason", "stages",
                                   "data_identity", "data_blocker", "not_requested", "pilot_stages")
             if c.get(k) not in (None, {}, [])} for c in ledger["checks"] if c["id"] not in executing]
    ran = {r["claim"]: r for r in ledger["completion"]["claims"]}
    busy = ("\n=== STILL RUNNING (long executions; each ends on its own and is reported then — never plan a duplicate of "
            "one, and never plan for a claim only a running check decides) ===\n"
            + "\n".join(f"- {c['id']} {c['kind']} for: {c['claim'][:120]!r}; covers {c.get('covers')}" for c in ledger["checks"]
                        if c["id"] in executing) + "\n") if executing else ""
    listed = "\n".join(f"- {cc['id'] + ' ' if cc.get('id') else ''}{cc.get('statement') or cc['quote']!r} ({cc.get('claim_type') or 'untyped'}): "
                       f"{cc['claim_status']}; the requested "
                       f"experiment: {ran[cc['quote']]['experiment']}" + "".join(
                           f" [{b['item']}: {b['blocker']}{' ' + b['class'] if b.get('class') else ''}, asserted by the "
                           f"{b['basis']}{'; NOT verified: ' + b['unverified'][:160] if b.get('unverified') else ''}"
                           f"{'; the search budget was spent when it was planned, which shows nothing about the data' if b.get('budget_spent') else ''}]"
                           for b in ran[cc["quote"]]["not_run"][:6]) for cc in ledger["central_claims"] if cc["quote"] in ran)
    rests = "\n".join(f"- {c['id']} {c['status']}: tested as {d['tested_as'][:160]!r}; the claim may hold under "
                      f"{d['alternative'][:200]!r}" + (f" (the paper: {d['printed'][:120]!r})" if d.get("printed") else "")
                      for c in ledger["checks"] if (c.get("audit") or {}).get("verdict") == "DEPENDS"
                      for d in c["audit"]["depends_on"])
    choices = "\n".join(f"- {d['printed'][:160]!r} (p{d['page']}): " + "; ".join(
        f"{u['checks']}: {u['used'][:120]!r}{' (changes the claim)' if u['changes_claim'] else ''}" for u in d["choices"])
        for d in ledger.get("definition_choices") or [])
    return ("\n=== FOLLOW-UP ROUND (every planned check has ended, or is a long run still going: see STILL RUNNING) ===\n"
            f"Main claims, and what was found:\n{listed}\nStill undecided:\n"
            + ("\n".join(f"- {cc['id'] + ' ' if cc.get('id') else ''}{cc['quote']!r}: {cc['claim_status']} (checks {', '.join(cc['checks']) or 'none'}; "
                         f"why unchecked: {cc.get('why_unchecked') or '-'})" for cc in undecided) or "- none") + busy
            + "\n=== WHAT EACH CHECK FOUND (harness statuses and reasons) ===\n"
            + json.dumps(done, ensure_ascii=False, indent=1)[:20_000]
            + "\nA check with a `data_blocker` could not acquire its data: its class says whether the source is missing, "
              "inaccessible, empty, failed validation, or a network fault of this run; another registry record, another file "
              "of the same record, or the same source again (after a transient fault) is a legitimate follow-up.\n"
              "A blocker the PLANNER asserted (not the harness) is re-examined, not repeated: a dataset or model never searched "
              "for is searched (discover); a public, ungated one is acquired; a `compute` blocker that no run measured is "
              "tested by a check (the harness measures a pilot and records a RESOURCE BLOCKER if it cannot fit); a "
              "`protocol` blocker whose missing details a script could declare as deviations is attempted.\n"
            + (f"\n=== ONE PRINTED DEFINITION, SEVERAL CHOICES (checks that applied the same sentence of the paper "
               f"differently; a follow-up may compute both as `readings` in one check) ===\n{choices}\n" if choices else "")
            + (f"\n=== FAILURES AN INDEPENDENT AUDIT FOUND RESTING ON A READING OR CHOICE THE PAPER LEAVES OPEN (not "
               f"counted against the claim; a check that computes the claim under the alternative too — `readings`, or a "
               f"certificate's `literal` — decides it) ===\n{rests}\n" if rests else "")
            + "\nPropose NEW checks only (ids F1, F2, ...), up to the budget, for (a) the undecided claims above, where a "
              "different route, a cited public artifact to acquire, or a narrower but still paper-faithful test can decide "
              "what the first round could not, and (b) any headline claim of the abstract, the contribution list or the "
              "conclusion that the list above does not contain (re-read them). Repeat nothing that already ran. "
              "`central_claims` lists ONLY those claims: for (a) by their id (K1, ...) when the list gives one, else by the "
              "same quote; for (b) as `new_claims` (each written as the claim extractor writes one: quote, statement, "
              "claim_type, scope, assumptions, required_evidence — plus its checks) when the claims above carry ids, else as a "
              "new verbatim quote with its scope in `central_claims`; each with the new check ids or a concrete "
              "`why_unchecked` naming the blocker. A claim may also list the id "
              "of a check above (C1, ...) that already ran the experiment it names: that check's `covers` count for the claim, "
              "so a scope item it ran is never written as an omission. Proposing nothing is correct when nothing more can be "
              "decided.\n")


def _data_text(x: _Ctx, cid: str) -> str:
    """What the harness acquired for a check (read-only at /work/data), as the manifest records it."""
    d = state.read_json(x.root / "checks" / cid / "data.json")
    if not d:
        return ""
    files = "\n".join(f"  {f['path']} ({f['bytes']} bytes)" + (f" head: {f['head'][:300]!r}" if f.get("head") else "")
                      for f in d.get("files", [])[:150])
    return (f"\n=== ACQUIRED DATA (read-only under {execute.DATA_MOUNT}/<n>/, one dir per source; sha256 in the "
            f"manifest) ===\n" + json.dumps([{k: s.get(k) for k in ("source", "dir", "failure_class", "admitted_files", "rejected",
                                                                    "missing", "unmatched_include", "packed", "recovery",
                                                                    "landing", "revision", "truncated") if s.get(k)}
                                             for s in d.get("sources", [])], ensure_ascii=False)[:6000]
            + f"\n{d.get('n_files', 0)} files, {d.get('bytes', 0)} bytes:\n{files}\n"
            + ("NOT COMPLETE — what the experiment needs and did not get: " + "; ".join(execute.data_gaps(d))[:1500] + "\n"
               if execute.data_gaps(d) else "")
            + "".join(f"THE RECORD ALSO LISTS (released, but not requested by this plan, so not under {execute.DATA_MOUNT}/{k}/): "
                      + ", ".join(u["not_requested"][:40]) + " — a REFEREE_DATA line says 'not acquired', never 'not released'\n"
                      for k, u in execute.unrequested(x.root, d).items())
            + "".join(f"EXCLUDED BY THE PLAN from source {i} ({e['pattern']}): {e['why']}\n"
                      for i, s in enumerate(d.get("plan") or []) for e in s.get("exclude") or [])
            + ("RELEASED RECORD TEXT (code, notebooks, README; quote-only, never run; Read it under "
               f"{(x.root / 'checks' / cid / 'record_src').as_posix()}/, cite a reading as record:<path>):\n"
               + "\n".join(f"  {r['path']} ({r['bytes']} bytes)" for r in d["record_src"][:60]) + "\n"
               if d.get("record_src") else ""))


def _step(x: _Ctx, c: dict) -> list[dict]:
    """The tasks owed for one check now (starting its execution when it is ready)."""
    cid, kind = c["id"], c["kind"]
    cdir = x.root / "checks" / cid
    rounds = 1 + x.cfg.max_revisions
    o = state.read_json(cdir / "outcome.json")
    # An approved script that could not start, or whose seeded runs varied nothing (execute.varied_nothing), goes back
    # to its author with the reason, not a verdict; its outcome is kept beside the next round's.
    if o and (o.get("setup_error") or o.get("revisable")) and kind in SCRIPT_KINDS:
        r0 = max((r for r in range(1, rounds + 1)
                  if (x.sealed(f"verify:{cid}.{r}") or {}).get("verdict") == "APPROVE"), default=0)
        if 0 < r0 < rounds:
            (cdir / f"setup.{r0}.txt").write_text(o.get("setup_log") or o.get("revisable") or o.get("reason", ""),
                                                  encoding="utf-8")
            (cdir / "outcome.json").replace(cdir / f"outcome.setup.{r0}.json")
            (cdir / "exec.json").unlink(missing_ok=True)
            o = None
    if o or (cdir / "exec.json").exists():
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
    if kind == "ARITHMETIC":
        v = x.sealed(f"verify:{cid}.1")
        if v is None:
            return [_verify_task(x, c, 1, spec, json.dumps(
                {k: c[k] for k in ("operands", "expression", "target")}, ensure_ascii=False, indent=1), "")]
        if v["verdict"] == "APPROVE":
            out = reconcile.arithmetic(*c["interval"], c["printed"])
            if out["status"] == "ARITHMETIC_CONTRADICTION":   # asserted only once the page images agree
                calc = {"operands": c["operands"], "target": {"quote": (c["target"] or {}).get("quote") or (
                    c["target"] or {}).get("row", ""), "value": c["printed"]}}
                items = _calc_items(x, cid, calc)
                chk = _image_check(x, f"vision:{cid}", items)
                if chk is None:
                    return _vision_task(x, f"vision:{cid}", items) or _terminal(
                        cdir, c, "INCONCLUSIVE", "the extracted numbers disagree, but they could not be located on "
                        "the page image, so no arithmetic error is asserted")
                out = ({**out, "image_check": chk, "reason": out["reason"] + "; every number was read off the page "
                        "image and agrees with the extraction"} if chk["agrees"] else
                       {"status": "INCONCLUSIVE", "image_check": chk, "reason": "the extracted numbers disagree, but the "
                        f"page image disagrees with the extraction ({chk['disagree']}): no error is asserted from "
                        "garbled text"})
        else:
            out = {"status": "NOT_CHECKABLE", "reason": f"verifier {v['verdict']}: {v['required_changes'] or v['notes']}"}
        state.write_json(cdir / "outcome.json", {"check": cid, "kind": kind, "evidence": "PAPER_ARITHMETIC",
                                                 "authorized": True, **out})
        return []
    if c.get("acquire"):
        data = state.read_json(cdir / "data.json") or {}
        if not data.get("fetched_at"):
            x.waiting.add(cid)                 # the data is being acquired: the author writes against it
            return []
        if (blk := execute.data_blocker(c["acquire"], data)):
            return _data_blocked(cdir, c, blk)
    if x.sealed(f"gen:{cid}.1") is None and execute.script_env(x.cfg, x.root, kind, c["repo_attributed"])[1] is None:
        x.waiting.add(cid)                     # its environment is still building: drafts could not run yet
        return []
    last = ""
    for r in range(1, rounds + 1):
        g = x.sealed(f"gen:{cid}.{r}")
        if g is None:
            prev = x.sealed(f"verify:{cid}.{r - 1}") if r > 1 else None
            setup = cdir / f"setup.{r - 1}.txt"
            revision = (
                f"\n=== REVISION {r}: the harness's run of the previous script returned it to you ===\nFix what "
                f"it names (a module missing from the environment: declare it on a `# REFEREE_PACKAGES:` line; "
                f"a path, argument or code error; a data file named differently than you assumed — look at the "
                f"acquired-data manifest). Never fix it by weakening the claim. The error (result lines masked):\n"
                f"{setup.read_text(encoding='utf-8')[-2500:]}\n"
                f"--- the script ---\n{(cdir / f'script.{r - 1}.py').read_text(encoding='utf-8')}\n"
                if r > 1 and setup.exists() else "" if not prev else
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
            env_now = execute.script_env(x.cfg, x.root, kind, c["repo_attributed"])[1] or {}
            return [x.task(f"gen:{cid}.{r}", "gen", _template(
                "gen", title=x.meta["title"], pages_dir=x.pages_dir, check_id=cid, kind=kind, claim=c["claim"],
                spec=spec + extra + _data_text(x, cid), contract=_section("contracts", f"gen {kind}"), metric=metric,
                environment=f"{env} (built: {env_now.get('detail', 'pending')[:300]})", host=execute.host_facts(x.cfg),
                required=", ".join(REQUIRED[kind]), max_tries=x.cfg.max_tries, try_cmd=try_cmd, revision=revision))]
        if g.get("refused") and g.get("by") == "harness":
            return _terminal(cdir, c, "INCONCLUSIVE", f"the harness did not accept the script author's final answer for "
                             f"gen:{cid}.{r} (these errors survived every seal attempt): {g['notes'][:400]}; a fault of "
                             "this run, nothing about the paper; the check may be reopened")
        if g.get("refused"):
            return _terminal(cdir, c, "NOT_CHECKABLE", f"the script author refused: {g['notes'][:400]}",
                             by_model=True)
        if (cdir / f"setup.{r}.txt").exists():
            last = (cdir / f"setup.{r}.txt").read_text(encoding="utf-8")[-400:]
            continue                                             # it could not run: the next round revises it
        v = x.sealed(f"verify:{cid}.{r}")
        if v is None:
            # No verifier sees a script nobody has run: the harness runs it once (seed 0, results
            # masked). A script that fails there goes back to its author with the error.
            sm = execute.smoke(x.cfg, x.root, cid, r, kind, c["repo_attributed"])
            if sm is None:
                x.waiting.add(cid)
                return []
            if sm.get("failed") and not sm.get("infra_error"):
                (cdir / f"setup.{r}.txt").write_text(f"{sm['failure']}\n--- stderr (tail) ---\n{sm['stderr'][-1500:]}",
                                                     encoding="utf-8")
                last = sm["failure"][-400:]
                continue
            if sm.get("schema"):          # it ran to completion, but its result lines break the contract
                (cdir / f"setup.{r}.txt").write_text(
                    "RESULT SCHEMA: the run completed, but its REFEREE_RESULT lines do not match the contract (values "
                    "stay masked): " + "; ".join(sm["schema"]) + "\nEvery declared unit prints its own result line "
                    "with that exact `stage`; with readings, every reading prints `reading` and `cohort` in every "
                    f"stage.\n--- stdout (tail, results masked) ---\n{sm.get('stdout', '')[-1500:]}", encoding="utf-8")
                last = "result schema: " + "; ".join(sm["schema"])[:380]
                continue
            tries = (cdir / "tries.jsonl").read_text(encoding="utf-8")[-6000:] if (cdir / "tries.jsonl").exists() else "(none)"
            proposal = json.dumps({"script": (cdir / f"script.{r}.py").read_text(encoding="utf-8"), **{
                k: g.get(k) for k in ("runs", "runs_quote", "metric", "outputs", "bindings", "deviations",
                                      "checked_statement", "premise_argument", "revisions") if g.get(k) is not None}},
                ensure_ascii=False, indent=1)
            smoke = f"(not run: {sm['skipped']})" if sm.get("skipped") else (
                     f"returncode {sm.get('returncode')}, {sm.get('seconds')}s"
                     + (" (stopped at the draft time limit: it was still running)" if sm.get("timed_out") else "")
                     + (f", infrastructure: {sm['infra_error']}" if sm.get("infra_error") else "")
                     + f"\n--- stdout (result lines masked) ---\n{sm.get('stdout', '')[-2000:]}\n--- stderr ---\n"
                     f"{sm.get('stderr', '')[-1500:]}")
            return [_verify_task(x, c, r, spec + _data_text(x, cid), proposal, tries, smoke)]
        if v["verdict"] == "APPROVE":
            runs, source = run_count(x.cfg, c, g)
            devs, crit = _two_keys(c, g, v)
            check = {**c, "runs": runs, "runs_quote": g.get("runs_quote", "") if source == "paper" else "",
                     "runs_source": source, "stochastic": g.get("stochastic"), "seed_flow": g.get("seed_flow"),
                     "readings": _merge_readings(c.get("readings"), g.get("readings")),
                     "script_sha256": g["script_sha256"], "metric": g.get("metric", c["metric"]), "criterion": crit,
                     "deviations": devs, "premise_argument": g.get("premise_argument", ""), "fidelity": g.get("fidelity") or [],
                     "revisions": [x.sealed(f"gen:{cid}.{k}").get("revisions") for k in range(2, r + 1)
                                   if (x.sealed(f"gen:{cid}.{k}") or {}).get("revisions")],
                     "approval": {"approved": True, "script_sha256": v["script_sha256"],
                                  "notes": v.get("notes", "")[:1000]}}
            shutil.copyfile(cdir / f"script.{r}.py", cdir / "script.py")
            # A script that cannot use the GPU never holds it (execute.wants_gpu): it runs beside a GPU run, not after it.
            check["gpu"] = execute.wants_gpu((cdir / "script.py").read_text(encoding="utf-8"), (x.checkout / ".git").is_dir())
            return _start(x, check)
        if v["verdict"] == "UNCHECKABLE" or r == rounds:
            why = "unCheckable" if v["verdict"] == "UNCHECKABLE" else f"{rounds} rounds rejected"
            return _terminal(cdir, c, "NOT_CHECKABLE", f"verifier: {why}: {(v['required_changes'] or v['notes'])[:400]}",
                             by_model=True)
    if all(x.sealed(f"gen:{cid}.{r}") is not None for r in range(1, rounds + 1)):
        return _terminal(cdir, c, "INCONCLUSIVE", f"the script failed in the harness's run in each of {rounds} rounds; "
                         f"last error: {last}")
    return []


def _supplied(c: dict, by: str = "planner") -> dict:
    """The deviation a REFEREE-chosen decision criterion is: a qualitative claim tested by a relation, rival, baseline or
    threshold someone chose speaks for that criterion, never for the claim as printed. A compatibility test's per-run
    condition is the harness's own protocol for an engineering claim (invariant 23), recorded as such: it changes nothing
    of that claim and speaks for compatibility only (the plan seal never lets it stand for a performance claim)."""
    if c.get("test") == "compatibility":
        return {"printed": "", "page": None, "changes_claim": False, "supplied_criterion": True,
                "used": f"REFEREE's compatibility condition ({(c.get('target') or {}).get('relation') or 'per run'}) in "
                        "every run: the component integrates, runs and trains; it speaks for compatibility only",
                "why": "an engineering claim is tested by a compatibility test whose per-run condition the harness defines"}
    return {"printed": "", "page": None, "changes_claim": True, "supplied_criterion": True, **({"changes_claim_by": by} if by != "planner" else {}),
            "used": f"REFEREE's {by} supplied the decision criterion "
                    f"({(c.get('target') or {}).get('relation') or c.get('metric') or 'the compared output'}) for a claim "
                    "whose sentence states no such comparison or number",
            "why": "the claim is qualitative; any threshold, rival or baseline is REFEREE's, so a result speaks for that "
                   "criterion and never for the claim as the paper states it"}


def _two_keys(c: dict, g: dict, v: dict) -> tuple[list[dict], str]:
    """The approved check's deviations and criterion with the verifier's independent key applied: a deviation either key
    calls claim-changing is claim-changing, and a criterion either key calls supplied is supplied (only ever downward)."""
    devs = [dict(d) for d in g.get("deviations") or []]
    for i in v.get("claim_changing") or []:
        if 0 <= i < len(devs) and not devs[i].get("changes_claim"):
            devs[i].update(changes_claim=True, changes_claim_by="verifier")
    crit = c.get("criterion", "")
    if c.get("kind") != "CERTIFICATE" and crit != "supplied" and v.get("criterion") == "supplied":
        devs.append(_supplied(c, "verifier"))
        crit = "supplied"
    return devs, crit


def run_count(cfg: state.Config, c: dict, g: dict) -> tuple[int, str]:
    """(runs, where the count came from). Recomputing from released files is deterministic: one run.
    An engineering compatibility test is SH_REPLICATES runs of one configuration, each required to
    meet its condition (the paper's run count belongs to its performance experiments). A pipeline
    its author declares deterministic runs twice (the second must repeat the first; reruns are never
    replicates). An experiment is decided over independent seeded replicates: never fewer than the
    paper states, and at least enough for a noise band (more is never a downscale)."""
    if c["kind"] == "RELEASED_DATA":
        return 1, "deterministic"
    if c["kind"] != "RECONSTRUCTION":
        return g["runs"], "paper" if g.get("runs_quote") else "referee"
    if c.get("test") == "compatibility":
        return cfg.replicates, "compatibility"
    if g.get("stochastic") is False:
        return 2, "deterministic"
    runs = max(g["runs"], cfg.replicates)
    return runs, "paper" if g.get("runs_quote") else ("referee_floor" if runs > g["runs"] else "referee")


def _merge_readings(a, b) -> list:
    """The planner's readings, then any further reading the script author found (by name)."""
    out = list(a or [])
    return out + [r for r in b or [] if r["name"] not in {o["name"] for o in out}]


def reopen(cfg: state.Config, pid: str, cid: str, why: str) -> dict:
    """The operator reopens a check that ended WITHOUT a scientific finding (a refusal, a blocker, an inconclusive
    run) after its cause was fixed in the harness. The old outcome is kept beside it (outcome.reopened.N.json);
    the check's script-writing seals are withdrawn so its tasks are owed again. A status that says what was found
    (support, a failure, a violation) is never reopened: a result is not re-rolled until it reads differently."""
    root = state.pdir(cfg, pid)
    if cid == "plan:2":
        return _replan(cfg, pid, root, why)
    if cid == "report":            # the report alone is written again after a fix of how it is computed; no outcome moves
        with state.lock(root / ".lock"):
            seals = state.read_json(root / "seals.json", {}) or {}
            if "report" not in seals:
                return {"error": "no sealed report to withdraw"}
            n = len(list(root.glob("review.reopened.*.md"))) + 1
            for src, dst in ((root / "sealed" / "report.json", root / "sealed" / f"report.withdrawn.{n}.json"),
                             (root / "review.md", root / f"review.reopened.{n}.md")):
                if src.exists():
                    src.replace(dst)
            seals.pop("report")
            state.write_json(root / "seals.json", seals)
            tried = state.read_json(root / "attempts.json", {}) or {}
            state.write_json(root / "attempts.json", {k: v for k, v in tried.items() if k != "report"})
            state.append_jsonl(root / "log.jsonl", {"event": "reopen", "check": "report", "why": why[:500]})
            return {"reopened": "report", "kept": f"review.reopened.{n}.md"}
    with state.lock(root / ".lock"):
        cdir = root / "checks" / cid
        o = state.read_json(cdir / "outcome.json")
        if not o:
            return {"error": f"{cid} has no outcome to reopen"}
        if o.get("status") not in ("NOT_CHECKABLE", "BLOCKED", "INCONCLUSIVE"):
            return {"error": f"{cid} is {o.get('status')}: a check that found something is never reopened"}
        n = len(list(cdir.glob("outcome.reopened.*.json"))) + 1
        (cdir / "outcome.json").replace(cdir / f"outcome.reopened.{n}.json")
        seals = state.read_json(root / "seals.json", {}) or {}
        # A RESOURCE BLOCKER is a fact about how the host ran an approved script, never about the script: the same
        # approved script runs again (its gen/verify seals kept, its completed seeds reused from seeds.jsonl), so a
        # measurement is never re-rolled by rewriting the script. Anything else is written and approved again.
        keep = bool(o.get("resource")) and o.get("authorized") is not False and (cdir / "script.py").exists()
        gone = [t for t in seals if t == f"audit:{cid}"] + ([] if keep else [
            t for t in seals if t.split(":", 1)[0] in ("bind", "gen", "verify") and _check_of(t) == cid])
        gone += [t for t in ("report", "compare") if t in seals]   # written before this check was redone: another ledger
        for t in gone:
            seals.pop(t)
            (root / "sealed" / f"{_safe(t)}.json").unlink(missing_ok=True)
        state.write_json(root / "seals.json", seals)
        tried = state.read_json(root / "attempts.json", {}) or {}       # the redone task starts with its whole budget: the
        state.write_json(root / "attempts.json", {k: v for k, v in tried.items() if k not in gone})   # last attempt seals leniently
        for f in ([cdir / "exec.json"] if keep else
                  [*cdir.glob("smoke.*.json"), *cdir.glob("setup.*.txt"), cdir / "exec.json", cdir / "check.json", cdir / "script.py"]):
            f.unlink(missing_ok=True)
        if not keep and (o.get("data_blocker") or execute.data_gaps(state.read_json(cdir / "data.json") or {})):
            (cdir / "data.json").unlink(missing_ok=True)       # a failed, cut or partial acquisition is tried again
            shutil.rmtree(cdir / "record_src", ignore_errors=True)
        state.append_jsonl(root / "log.jsonl", {"event": "reopen", "check": cid, "was": o.get("status"), "why": why[:500],
                                                 **({"same_approved_script": True} if keep else {})})
        return {"reopened": cid, "was": o.get("status"), "seals_withdrawn": gone, **({"same_approved_script": True} if keep else {})}


def _replan(cfg: state.Config, pid: str, root: Path, why: str) -> dict:
    """The operator withdraws a follow-up plan that was made on what the harness wrongly told it (a failed search read
    as a search, a budget it had not spent), so the follow-up round is planned again once every first-round check has
    ended. Only while none of the follow-up's checks has found anything: each one's folder is kept aside
    (checks/<id>.withdrawn.N), never deleted; a check that found something is never re-rolled."""
    with state.lock(root / ".lock"):
        follow = _sealed(root, "plan:2")
        if follow is None:
            return {"error": "no sealed follow-up plan to withdraw"}
        found = [c["id"] for c in follow["checks"] if (state.read_json(root / "checks" / c["id"] / "outcome.json") or {}).get(
            "status") not in (None, "NOT_CHECKABLE", "BLOCKED", "INCONCLUSIVE")]
        if found:
            return {"error": f"follow-up check(s) {found} found something: a follow-up plan with a finding is never withdrawn"}
        if any((root / "checks" / c["id"] / "exec.json").exists() and not (root / "checks" / c["id"] / "outcome.json").exists()
               for c in follow["checks"]):                 # an ended check keeps its exec.json: only one without an outcome runs
            return {"error": "a follow-up check is executing: stop it first (run.py stop), then withdraw the plan"}
        moved = []
        for c in follow["checks"]:
            d = root / "checks" / c["id"]
            if d.exists():
                n = len(list(d.parent.glob(f"{c['id']}.withdrawn.*"))) + 1
                d.rename(d.with_name(f"{c['id']}.withdrawn.{n}"))
                moved.append(c["id"])
        seals = state.read_json(root / "seals.json", {}) or {}
        gone = [t for t in seals if t in ("plan:2", "report") or (t.split(":", 1)[0] in ("bind", "gen", "verify", "audit")
                                                                  and _check_of(t) in {c["id"] for c in follow["checks"]})]
        n = len(list((root / "sealed").glob("plan__2.withdrawn.*.json"))) + 1
        kept = root / "sealed" / f"plan__2.withdrawn.{n}.json"
        (root / "sealed" / "plan__2.json").replace(kept)
        for t in gone:
            seals.pop(t)
            if t != "plan:2":
                (root / "sealed" / f"{_safe(t)}.json").unlink(missing_ok=True)
        # Still sealed under its own name: its checks are set aside, never its claims (report.merged accounts for them).
        seals[f"plan:2.withdrawn.{n}"] = state.sha256(kept.read_bytes())
        state.write_json(root / "seals.json", seals)
        tried = state.read_json(root / "attempts.json", {}) or {}
        state.write_json(root / "attempts.json", {k: v for k, v in tried.items() if k not in gone})
        state.append_jsonl(root / "log.jsonl", {"event": "replan", "withdrawn": "plan:2", "checks_set_aside": moved,
                                                 "why": why[:500]})
        return {"withdrawn": "plan:2", "checks_set_aside": moved, "seals_withdrawn": gone}


def _check_of(tid: str) -> str:
    """The check id a bind/gen/verify task id belongs to (bind:C1, gen:C1.2, verify:C1.2)."""
    return tid.split(":", 1)[1].rsplit(".", 1)[0]


def _data_blocked(cdir: Path, c: dict, blk: list[dict]) -> list:
    """A check whose required data was not admitted ends here: no script is written against data that
    was not acquired (it would substitute a surrogate). A source that is missing, inaccessible, empty or
    failed validation is a documented DATA BLOCKER (BLOCKED); a network or host failure, or a fault of
    this harness, is INCONCLUSIVE — a fact about this run, not about the source."""
    classes = sorted({b["class"] for b in blk})
    infra = set(classes) <= {"infrastructure", "transient", "bug"}
    why = "; ".join(f"{b['source']} [{b['class']}]: {b['detail']}" + (f" (rejected: {'; '.join(b['rejected'])})" if b["rejected"] else "")
                    for b in blk)
    state.write_json(cdir / "outcome.json", {
        "check": c["id"], "kind": c["kind"], "status": "INCONCLUSIVE" if infra else "BLOCKED", "authorized": False,
        "reason": ("DATA ACQUISITION FAILED" if infra else "DATA BLOCKER") + f" ({', '.join(classes)}): {why}. No script was "
                  "written against data that was not acquired: a simulation would be a different experiment.",
        "data_blocker": blk, "rule": "acquisition and content validation before any experiment"})
    return []


def _terminal(cdir: Path, c: dict, status: str, reason: str, by_model: bool = False) -> list:
    state.write_json(cdir / "outcome.json", {"check": c["id"], "kind": c["kind"], "status": status,
                                             "authorized": False, "reason": reason,
                                             **({"reason_by": "model"} if by_model else {})})
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


_AUDIT_LINES = 60   # ponytail: result lines shown to an auditor (failing ones first); every line is in execution.jsonl


def _audits(x: _Ctx, plan: dict) -> list[dict]:
    """An independent audit owed for every failure about the printed claim (report.needs_audit): a fresh reader, shown the
    failing instances unmasked, looks for a premise the paper states that they miss, a reading of open text, or a choice
    REFEREE supplied that the failure rests on. Like the critic, it can only lower: a failure it finds resting on such a
    thing reads as about a changed claim, never as support, and is shown beside what it rests on (invariant 16)."""
    return [_audit_task(x, c) for c in plan["checks"]
            if report.needs_audit(x.root, c) and x.sealed(f"audit:{c['id']}") is None]


def _audit_task(x: _Ctx, c: dict) -> dict:
    cdir = x.root / "checks" / c["id"]
    o, run = state.read_json(cdir / "outcome.json") or {}, state.read_json(cdir / "check.json") or {}
    log = x.root / "execution.jsonl"
    rows = [json.loads(ln) for ln in log.read_text(encoding="utf-8").splitlines() if ln.strip()] if log.exists() else []
    lines = [f"seed {r.get('seed')}: {ln.strip()}" for r in rows if r.get("target") == c["id"] and r.get("mode") == "evidence"
             and r.get("script_sha256") == run.get("script_sha256")
             for ln in (r.get("stdout") or "").splitlines() if ln.startswith("REFEREE_RESULT")]
    bad = lambda ln: bool(re.search(r'"violated":\s*1\b|"literal":\s*"fails"', ln))
    lines = sorted(lines, key=lambda ln: not bad(ln))[:_AUDIT_LINES]
    shown = {k: o.get(k) for k in ("status", "reason", "rule", "admissible", "literal", "relation", "margin", "n")
             if o.get(k) not in (None, "")}
    shown["stages"] = {s: {k: p.get(k) for k in ("status", "reason", "margin", "n")} for s, p in (o.get("stages") or {}).items()
                       if p.get("status") != "RELATION_HOLDS"}
    script = cdir / "script.py"
    return x.task(f"audit:{c['id']}", "audit", _template(
        "audit", title=x.meta["title"], pages_dir=x.pages_dir, check_id=c["id"], kind=c["kind"], status=o.get("status"),
        claim=c["claim"], spec=_spec_text(c), outcome=json.dumps(shown, ensure_ascii=False, indent=1)[:6000],
        script=script.as_posix() if script.exists() else "(no script: the authors' documented command)",
        deviations=json.dumps([{"index": i, **{k: d.get(k) for k in ("printed", "used", "why", "changes_claim")}}
                               for i, d in enumerate(run.get("deviations") or [])], ensure_ascii=False, indent=1)[:8000],
        results="\n".join(lines)[:12_000] or "(no result lines recorded)"),
        (script,) if script.exists() else ())


def _verify_task(x: _Ctx, c: dict, r: int, spec: str, proposal: str, tries: str, smoke: str = "") -> dict:
    return x.task(f"verify:{c['id']}.{r}", "verify", _template(
        "verify", title=x.meta["title"], pages_dir=x.pages_dir, check_id=c["id"], kind=c["kind"], claim=c["claim"],
        spec=spec, rules=_section("contracts", f"verify {c['kind']}"), proposal=proposal, tries=tries,
        smoke=smoke or "(not applicable)", decide=_section("contracts", f"decide {c['kind']}") or "(see above)"))


def _spec_text(c: dict) -> str:
    parts = []
    if c.get("test"):
        parts.append(f"Test: {c['test'].upper()} — " + (
            "an ENGINEERING COMPATIBILITY test: show the component integrates, runs and trains in one configuration; "
            "the condition below must hold in every run. It is NOT a performance comparison: no baselines to beat, "
            "no tuning sweep, no benchmark grid; its result speaks only for compatibility." if c["test"] == "compatibility"
            else "a PERFORMANCE comparison over independent seeded replicates, at the paper's stated protocol."))
    if c.get("basis"):
        parts.append(f"Evidence basis: {c['basis']} (" + {
            "published_results": "an AUDIT of the authors' released result files: re-derive the printed number from "
                                 "the results they published; nothing is re-run or re-scored",
            "predictions": "a RECOMPUTATION of the metric from released per-item predictions or scores",
            "fresh_run": "a FRESH RUN (training, simulation) in the sandbox",
            "exact_instances": "exact instances", "paper_numbers": "the paper's own printed numbers"}.get(c["basis"], "")
            + ")")
    if c.get("covers") and c.get("kind") in ("AUTHOR_CODE", "RELEASED_DATA", "RECONSTRUCTION"):
        parts.append("Covers (the claim's scope items this check runs): " + json.dumps(c["covers"], ensure_ascii=False)
                     + " — every one is computed; a dataset among them that the script cannot load is named in its "
                       "REFEREE_DATA line's `missing` list (it then reads as not run), never dropped silently.")
    if c.get("target"):
        parts.append(f"Printed target: {json.dumps(c['target'], ensure_ascii=False)}")
    if c.get("define"):
        parts.append("The compared outputs, as the planner defined them (compute each exactly so): "
                     + json.dumps(c["define"], ensure_ascii=False))
    if c.get("readings"):
        parts.append("READINGS (the paper's text and the checkout's code define the compared quantity differently; "
                     "compute EVERY reading in the same run, on the same data and the same cohort, one result line per "
                     "reading and stage carrying `reading` and `cohort`; never choose one): "
                     + json.dumps(c["readings"], ensure_ascii=False))
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
    from .repo import restore
    restore(cfg, root)                     # a packed run resumes on its recorded commit
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
    return {"paper_id": pid, "phase": phase, "status": "workflow_finished" if phase == "done" else "waiting",
            "blocked_reason": blocked_reason(running), "running": running,
            "scientific_status": report.scientific_status(root), "completion": report.completion_line(root),
            "tasks": tasks}


def blocked_reason(running: list[str]) -> str:
    """Said whenever an execution is in flight, tasks owed or not: a workflow whose owed tasks no worker could answer must
    still keep polling the runs that are going (Oct-01 PPRM: it stopped while C8 and C9 ran)."""
    return f"executions running: {', '.join(running)}" if running else ""


def abandon(cfg: state.Config, pid: str, tid: str, why: str) -> dict:
    """The workflow ends a task no worker could answer (two workers, no sealable answer — a permission block, a crash):
    it is sealed as an honest nothing with the reason, so one stuck task never stalls the rest of the review. A check
    whose binding, script or approval is abandoned ends INCONCLUSIVE — a fault of this run, nothing about the paper — and
    may be reopened; an abandoned audit leaves its failure UNRESOLVED, so it is not counted (fail closed)."""
    root = state.pdir(cfg, pid)
    with state.lock(root / ".lock"):
        x = _Ctx(cfg, pid)
        if tid not in {t["id"] for t in _plan(x)[1]}:
            return {"error": f"'{tid}' is not a pending task"}
        role = tid.split(":")[0]
        rec = {**_EMPTY[role], "abandoned": why[:500]}
        out = root / "sealed" / f"{_safe(tid)}.json"
        state.write_json(out, rec)
        seals = state.read_json(root / "seals.json", {}) or {}
        seals[tid] = state.sha256(out.read_bytes())
        state.write_json(root / "seals.json", seals)
        if role in ("bind", "gen", "verify"):
            cid = _check_of(tid)
            c = next(k for k in x.plan()["checks"] if k["id"] == cid)
            if not (root / "checks" / cid / "outcome.json").exists():
                _terminal(root / "checks" / cid, c, "INCONCLUSIVE", f"no worker produced a sealable answer for {tid} ({why[:300]}): "
                          "a fault of this run, nothing about the paper; the check may be reopened")
        state.append_jsonl(root / "log.jsonl", {"event": "abandon", "task": tid, "why": why[:500]})
    return {"abandoned": tid}


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
                        + "\n- ".join(errors[:40]) + (f"\n- ... and {len(errors) - 40} more of the same kinds; fix "
                                                       "them all" if len(errors) > 40 else ""))


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


# ponytail: 30 main claims is a safety ceiling against a runaway list, not a target: the extractor is told to list every
# main claim and no more; past it the answer is refused (merge restatements and scope items), never cut unseen.
MAX_EXTRACTED = 30
# ponytail: 40 scope items per claim (methods, baselines, datasets, settings of one conclusion); more is refused, and on the
# last attempt the items past it are recorded with the claim (`scope_cut`) and reported as not tested — never cut unseen.
MAX_SCOPE = 40


def _seal_claims(x: _Ctx, tid: str, obj: dict, final: bool) -> dict:
    """The paper's main claims, extracted before any plan (a planner tests claims; it never makes them). Each keeps the
    words it is stated in, its type, its scope (one item per entry), the assumptions under which the paper claims it, the
    evidence the paper offers and what would decide it — every quote re-found. Ids K1..Kn are the harness's."""
    raw = [c for c in obj.get("claims") or [] if isinstance(c, dict)]
    out, dropped, errors = _validate_claims(x, raw, [], 1)
    for o in out:
        o.pop("_raw", None)
    if len(raw) > MAX_EXTRACTED:
        errors.append(f"{len(raw)} claims: at most {MAX_EXTRACTED} — a dataset, baseline, seed or case of one conclusion is its "
                      "scope, and a restatement is `also_stated`, never another claim")
    if not out:
        errors.append("no main claim was extracted: every paper states at least one result")
    _fail_or_drop(errors, final)
    return {"claims": out, "dropped": dropped, "notes": str(obj.get("notes") or "")[:2000]}


def _validate_claims(x: _Ctx, raw: list[dict], known: list[dict], first: int) -> tuple[list, list, list]:
    """(claims, dropped, errors): each claim re-found and typed, numbered K<first>.. after the `known` ones (a claim that
    restates a known one is refused)."""
    errors, out, dropped = [], [], []
    for c in raw[:MAX_EXTRACTED]:
        errs: list[str] = []
        h = _find(x, str(c.get("quote") or ""), errs, "claim quote")
        label = f"claim {str(c.get('quote') or '')[:50]!r}"
        ctype = _enum(c.get("claim_type"), CLAIM_TYPES)
        if not ctype:
            errs.append(f"{label}: `claim_type` is one of {list(CLAIM_TYPES)}")
        statement = re.sub(r"\s+", " ", str(c.get("statement") or "")).strip()
        if not 10 <= len(statement) <= 400:
            errs.append(f"{label}: `statement` is the claim in one plain sentence (10-400 characters)")
        scope = [p[:120] for s in c.get("scope") or [] if str(s).strip() for p in _parts(str(s))]
        if len(scope) > MAX_SCOPE:              # refused, never cut unseen; on the last attempt the cut is recorded
            errs.append(f"{label}: {len(scope)} scope items: at most {MAX_SCOPE} — list each method, dataset and setting once")
        scope, cut = scope[:MAX_SCOPE], scope[MAX_SCOPE:]
        if not scope:
            errs.append(f"{label}: `scope` lists every method, dataset, setting and metric the claim names (a theorem: itself)")
        req = str(c.get("required_evidence") or "").strip()
        if len(req) < 15:
            errs.append(f"{label}: `required_evidence` says what would decide the claim within its stated scope")
        quoted = lambda items, what: [
            {"quote": hit["quote"], "page": hit["page"], **{k: str(i.get(k) or "")[:300] for k in ("what", "name", "reading")
                                                          if i.get(k)}}
            for i in items if isinstance(i, dict) and (hit := _find(x, str(i.get("quote") or ""), errs, f"{label} {what}"))]
        also = [hit["quote"] for q in c.get("also_stated") or [] if (hit := _find(x, str(q), errs, f"{label} also_stated"))]
        assumptions = quoted(c.get("assumptions") or [], "assumption")
        evidence = quoted(c.get("evidence_in_paper") or [], "evidence_in_paper")
        interp = quoted(c.get("interpretations") or [], "interpretation")
        if any(not re.fullmatch(r"[a-z][a-z0-9_]{0,23}", i.get("name", "")) or len(i.get("reading", "")) < 10 for i in interp):
            errs.append(f"{label}: each interpretation has a short lowercase `name` and its `reading`")
        dup = h and next((o for o in known + out if report.same_claim(h["quote"], o["quote"])), None)
        if dup:
            errs.append(f"{label}: the same claim as {dup['id']} (one quote inside the other): list it once, the other "
                        "words in `also_stated`")
        if errs and (not h or not ctype or not scope or dup):
            dropped.append({"claim": str(c.get("quote") or "")[:200], "why": errs})
            errors += errs
            continue
        errors += errs
        out.append({"id": f"K{first + len(out)}", "quote": h["quote"], "page": h["page"], "also_stated": also,
                    "statement": statement[:400], "claim_type": ctype, "scope": scope, "assumptions": assumptions,
                    "evidence_in_paper": evidence, "required_evidence": req[:600], "interpretations": interp,
                    "_raw": raw.index(c), **({"scope_cut": cut} if cut else {}),
                    **({"sealed_with_errors": errs[:6]} if errs else {})})
    dropped += [{"claim": str(c.get("quote") or "")[:200], "why": [f"past the ceiling of {MAX_EXTRACTED} claims"]}
                for c in raw[MAX_EXTRACTED:]]
    return out, dropped, errors


def _claims_text(x: _Ctx) -> str:
    """The sealed main claims, as the planner and the follow-up round see them (fixed: never added to, dropped or retyped
    by a plan)."""
    ks = (x.sealed("claims") or {}).get("claims") or []
    return "\n".join(json.dumps({k: c.get(k) for k in ("id", "statement", "claim_type", "quote", "page", "scope",
                                                       "assumptions", "required_evidence", "interpretations") if c.get(k)},
                                ensure_ascii=False) for c in ks) or "(no claims were extracted)"


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


_parts = report.parts


def unaccounted_names(names: list[str] | None, include: list[str], exclude: list[dict]) -> list[str]:
    """The data files of a record's listing that `include` does not name and no `exclude` entry (with its reason) names;
    code, notebooks, READMEs and dot-paths are text or metadata, never data."""
    from .fetcher import _dot, _named, _textual
    pats = [e["pattern"] for e in exclude or []]
    return [n for n in names or [] if not _dot(n) and not _textual(n) and not _named(n, include) and not _named(n, pats)]


_SOURCE = re.compile(r"https?://[^\s\"'<>]+|hf://(?:datasets|models)/[\w.-]+/[\w.-]+(?:@[\w.-]+)?")


def _acquire(x: _Ctx, c: dict, errs: list[str], cid: str) -> list[dict]:
    """Public artifacts a check needs that the checkout does not ship. A source is cited by the paper or
    by a tracked checkout file — matched as printed: a line-break hyphen, a missing scheme, the punctuation
    of the sentence around it, a repository's own `www.` and record/records spelling, and a DOI for its
    record are the same citation, and the span as printed is kept with the readings it can
    mean — or it is a record the harness's own registry search returned for a dataset the paper names
    (`cited_in: "discovery"`). None may be a denied source."""
    out = []
    for s in [s for s in c.get("acquire") or [] if isinstance(s, dict)][:5]:    # ponytail: 5 sources per check
        src, cited = clean_source(str(s.get("source") or "")), str(s.get("cited_in") or "").strip()
        inc = [str(p)[:200] for p in s.get("include") or [] if str(p).strip()][:20]
        if not _SOURCE.fullmatch(src):
            errs.append(f"{cid}: acquire source {src[:120]!r} is neither an http(s) URL nor hf://datasets|models/owner/name")
            continue
        if any(d in src.lower() for d in x.cfg.deny_sources):
            errs.append(f"{cid}: acquire source {src[:120]!r} is a denied source")
            continue
        if not src.startswith("hf://") and "@" in src.split("://", 1)[1].split("/", 1)[0]:
            errs.append(f"{cid}: acquire source {src[:120]!r} carries user info before its host; refused")
            continue
        exc = [{"pattern": str(e.get("pattern") or "")[:200], "why": str(e.get("why") or "").strip()[:400]}
               for e in s.get("exclude") or [] if isinstance(e, dict) and str(e.get("pattern") or "").strip()][:20]
        if bad := [e["pattern"] for e in exc if len(e["why"]) < 10]:
            errs.append(f"{cid}: each `exclude` entry gives the reason this claim does not need those files: {bad[:5]}")
        rec = {"source": src, "include": inc, "cited_in": cited, "why": str(s.get("why") or "")[:400],
               "required": s.get("required") is not False, **({"exclude": exc} if exc else {})}
        # Which of the check's scope items this source holds the data for: the run must then show, per item, that what it
        # loaded is that data (a REFEREE_DATA line naming it in `covers`), or say it is missing — so a download plan that
        # left a required file out reads as an item not run, never as covered (validated against the experiment).
        covers = {flat(p) for x_ in c.get("covers") or [] for p in _parts(str(x_))}
        serves = [str(v)[:120] for v in s.get("serves") or [] if str(v).strip()][:12]
        if (stray := [v for v in serves if flat(v) not in covers]):
            errs.append(f"{cid}: acquire source {src[:80]} `serves` {stray[:4]}, which are not in this check's `covers`")
        if serves:
            rec["serves"] = serves
        # Every data file the record's listing names is requested or excluded with a reason: a file left out unseen made
        # an acquisition read complete (Oct-01 transformer: 20 of 25, the plan's own `why` said 24). Only a listing the
        # planner was shown (`discover --files`) binds it; a listing the fetcher alone saw stays informational.
        names = execute.listed_names(x.root, src)
        if names is not None:
            rec["listed_at_seal"] = True
        if inc and (left := unaccounted_names(names, inc, exc)):
            errs.append(f"{cid}: the record {src[:80]} lists {len(left)} data file(s) this check neither requests nor excludes: "
                        f"{left[:12]}{' ...' if len(left) > 12 else ''} — add them to `include`, or list them in `exclude` "
                        "with the reason this claim does not need them")
        if cited == "discovery":
            did, named = str(s.get("discovery") or ""), str(s.get("named_in_paper") or "")
            hit = discover.returned(x.cfg, x.pid, did, src)
            if not hit:
                errs.append(f"{cid}: acquire source {src[:120]!r} is not a record that discovery {did or '(none given)'!r} "
                            "returned: run the discover command, then copy `source` from one of its candidates")
                continue
            if len(flat(named)) < 3 or flat(named) not in x.paper.flat:
                errs.append(f"{cid}: `named_in_paper` must be the paper's own words naming the dataset (verbatim), "
                            f"which the record {src[:80]!r} is claimed to be")
                continue
            rec.update(discovery=did, named_in_paper=named, found={k: hit[k] for k in (
                "registry", "title", "creators", "year", "doi", "license") if hit.get(k)})
        else:
            f = None if cited == "paper" else _repo_file(x, cited)
            book = x.paper if cited == "paper" else (Paper([f.read_text(encoding="utf-8", errors="replace")])
                                                     if f and f.is_file() else None)
            hit = book.cites(src) if book else None
            if not hit:
                errs.append(f"{cid}: acquire source {src[:120]!r} is not cited in {cited or '(nothing)'!r} (give cited_in: "
                            "'paper' or a tracked checkout path; a line-break hyphen, a missing scheme, and a DOI for "
                            "its record count as the same citation). A dataset the paper only NAMES is found with the discover "
                            "command and given as cited_in: 'discovery'")
                continue
            rec.update(cited_as=hit["span"][:300], cited_form=hit["form"], alternates=hit["variants"][:2])
        out.append(rec)
    return out


_COMPUTE = re.compile(r"\d[\d.,]*\s*(?:[kKmMbB]\b|billion|million)?[\s-]*(?:x\s*)?(?:[A-Z]?\d*\s*)?(?:gpus?|tpus?|"
                      r"a100s?|h100s?|v100s?|gpu[\s-]?hours?|hours?|days?|weeks?|cores?|nodes?|[gt]b\b|gib|tib|"
                      r"param(?:eter)?s?|flops?)", re.I)


def _measured(x: _Ctx, e: dict, errors: list[str], label: str, b: str) -> tuple[list, list]:
    """The checks an omission cites (`failed_checks`) as evidence of its blocker, sorted by what each one's record shows:
    (cap evidence, measured evidence). A check is evidence only for an item it covered (a measured run of one experiment
    says nothing about another); a fault of this run's network or host is evidence of nothing about the data or the host."""
    by_id = {c["id"]: c for c in (x.plan() or {}).get("checks", [])}
    caps, measured = [], []
    for k in [str(k) for k in e.get("failed_checks") or []]:
        o = state.read_json(x.root / "checks" / k / "outcome.json") or {}
        if e.get("item") and k in by_id and flat(e["item"]) not in {flat(s) for s in by_id[k].get("covers") or []}:
            errors.append(f"{label}: {k} did not run {e['item']!r} (its covers: {by_id[k].get('covers')}): what its run measured is "
                          "evidence only for what it ran — link that check to this claim, or give this item its own check")
            continue
        if o.get("data_blocker"):
            word = report.data_word(o["data_blocker"])
            if word == "fault":
                errors.append(f"{label}: {k} failed on a fault of this run's network, host or code "
                              f"({', '.join(sorted({d.get('class', '') for d in o['data_blocker']}))}), which shows nothing about "
                              "the data: acquire it again (a follow-up check), never give it up on that")
            (caps if word == "cap" else measured).append((k, word))
        elif str(o.get("reason", "")).startswith("RESOURCE BLOCKER"):
            (caps if report.resource_word(o.get("resource")) == "cap" else measured).append((k, o.get("resource") or "compute"))
    return caps, measured


def _blocker(x: _Ctx, e: dict, errors: list[str], label: str, caps_hit: tuple = ()) -> str:
    """A requested experiment or dataset that is not run names WHAT stops it, and rests on what the harness
    holds wherever it can (returns the error, "" if the blocker stands):
    - `data`: a registry search it ran for the dataset (and, if that returned candidates, why none is the
      dataset the paper names), or a check that failed to acquire it;
    - `credentials`: a hosted closed service (`service`: nothing to download), or the registry record of the
      `artifact` showing it gated or private — a public, ungated model or dataset is acquired, not given up;
    - `compute`: a check whose measured run exceeded this host (`failed_checks`, a RESOURCE BLOCKER), the
      registry's size of the `artifact` beyond the measured host's memory, or the paper's own words stating the
      compute it used (`paper_quote`) — never an estimate nobody measured.
    `protocol` and `other` are the planner's word, reported as such."""
    b, n0 = e.get("blocker"), len(errors)
    searched = x.cfg.allow_network and x.cfg.allow_data_search
    recs = {r["id"]: r for r in discover.records(x.cfg, x.pid)}
    cited = [d for d in e.get("discovery") or [] if d in recs and "results" in recs[d]]
    # A search counts only where a registry answered: one that failed (an HTTP error, a timeout) shows nothing.
    ids = [d for d in cited if any(not v.get("error") for v in recs[d]["results"].values())]
    if b in ("data", "credentials", "compute") and searched and cited and not ids:
        errors.append(f"{label}: every search it cites failed ({'; '.join(sorted({v['error'][:80] for d in cited for v in recs[d]['results'].values() if v.get('error')}))}): "
                      "a failed search shows nothing about the dataset or model — search again")
    art_src = str(e.get("artifact") or "").strip()
    art = next((h for d in ids if (h := discover.returned(x.cfg, x.pid, d, art_src))), None) if art_src else None
    capped, measured = _measured(x, e, errors, label, b) if b in ("data", "compute", "cap") else ([], [])
    if b not in BLOCKERS:
        errors.append(f"{label}: `blocker` is one of {list(BLOCKERS)} — what stops the requested experiment (data: no public "
                      "source could be found or read; credentials: a paid closed service or a gated record; compute: beyond "
                      "this host, measured; cap: a configured cap of this run that was reached (a time budget, the search or "
                      "check budget, the data cap); protocol: a detail the paper omits that no declared deviation can supply; other)")
    elif b == "cap":
        if not capped and not caps_hit:
            errors.append(f"{label}: a `cap` blocker rests on a configured cap this run reached — a check that ended at a time "
                          "budget, the data cap or a denied source (`failed_checks`), or the search or check budget spent — and "
                          "no configured cap was reached here: name the real blocker")
        e["cap"] = "; ".join([f"{k}: {w}" for k, w in capped] + list(caps_hit))[:300]
    elif b == "data" and capped and not measured:
        errors.append(f"{label}: {', '.join(k for k, _ in capped)} stopped at a configured cap of this run (the data cap or a "
                      "denied source), not at a fact about the data: its blocker is `cap`")
    elif b == "data" and searched:
        failed = [k for k, w in measured if w == "data"]
        if not ids and not failed:
            errors.append(f"{label}: a dataset given up as unobtainable needs `discovery` — the ids of the registry searches you "
                          "ran for it (the discover command) — or `failed_checks`: checks that tried to acquire it")
        elif ids and not failed and sum(len(r["candidates"]) for d in ids for r in recs[d]["results"].values()) \
                and not str(e.get("not_the_dataset") or "").strip():
            errors.append(f"{label}: the searches {ids} returned candidate records: acquire one (cited_in 'discovery'), or say "
                          "in `not_the_dataset` why none of them is the dataset the paper names")
    elif b == "credentials" and searched and e.get("service") is not True:
        if not art:
            errors.append(f"{label}: a `credentials` blocker is a hosted closed service (`service`: true — a paid API with no "
                          "public weights or data) or a gated artifact: `artifact` is a candidate a registry search returned "
                          "(`discovery` ids; search --registry huggingface-models for a model), whose record shows it gated or "
                          "private")
        elif not (art.get("gated") or art.get("private")):
            errors.append(f"{label}: {art['source']} is public and not gated in its registry record: acquire it (cited_in "
                          "'discovery'); credentials stop only a paid service or a gated or private record")
    elif b == "compute" and capped and not measured:
        errors.append(f"{label}: {', '.join(f'{k} ({w})' for k, w in capped)} stopped at a configured limit of this run "
                      "(SH_RUN_TIMEOUT_S, SH_CHECK_BUDGET_S, SH_MAX_DATA_GB), not at a measured limit of this host: its "
                      "blocker is `cap`")
    elif b == "compute":
        measured = [k for k, w in measured if w != "data"]
        quote = str(e.get("paper_quote") or "")
        h = execute.host(x.cfg) if art and art.get("size_bytes") else {}
        known = [m for m in (h.get("ram_mb"), h.get("vram_mb")) if isinstance(m, (int, float))]
        mem = max(known) * 2 ** 20 if known else None        # unmeasured memory is unknown, never zero
        if measured or (quote and x.paper.find(quote)[0] and _COMPUTE.search(quote)):
            pass
        elif quote and x.paper.find(quote)[0]:
            errors.append(f"{label}: `paper_quote` must be the paper's statement of the compute it used — a number with "
                          "its unit (GPUs, GPU hours or days, nodes, GB of memory, parameters): this sentence states none")
        elif art and art.get("size_bytes") and mem is None:
            errors.append(f"{label}: this host's memory could not be measured, so {art['source']}'s size decides nothing: "
                          "propose the check — its measured run is the compute evidence")
        elif art and art.get("size_bytes"):
            if art["size_bytes"] <= mem:
                errors.append(f"{label}: {art['source']} is {art['size_bytes'] / 2 ** 30:.1f} GB in its registry record, within this "
                              f"host's measured memory ({h.get('ram_mb')} MB RAM, GPU memory {h.get('vram_mb') or 'none'} MB): "
                              "propose the check — a measured run past the budget is the compute blocker (`failed_checks`)")
        else:
            errors.append(f"{label}: a `compute` blocker rests on a measurement: `failed_checks` (a check that ended in a RESOURCE "
                          "BLOCKER), `artifact` (a registry candidate whose size exceeds this host's measured memory), or "
                          "`paper_quote` (the paper's own words stating the compute it used, verbatim)")
    return "; ".join(errors[n0:])


def _enum(v, allowed: tuple) -> str:
    """A closed-vocabulary field: exactly one of `allowed`, else "" (never a default that switches a rule off)."""
    return v if isinstance(v, str) and v in allowed else ""


def _seal_plan(x: _Ctx, tid: str, obj: dict, final: bool) -> dict:
    errors, checks, dropped = [], [], []
    concern_ids = {c["id"] for c in x.concerns()}
    spent = discover._spent(discover.records(x.cfg, x.pid))   # searches this paper used: a fact beside `other`/`protocol`
    base = x.sealed("plan") if tid == "plan:2" else None      # a follow-up round adds to the first plan
    attributed = base["repo_is_authors"] if base else (x.checkout / ".git").is_dir() and obj.get("repo_is_authors") is True
    # Follow-up ids continue after every id the first round used (a check dropped there left a gap).
    used = [int(m.group(1)) for c in (base or {}).get("checks", []) + (base or {}).get("dropped", [])
            if (m := re.fullmatch(r"C(\d+)", str(c.get("id") or c.get("check") or "")))]
    cap, n0 = (x.cfg.max_followup_checks, max(used + [x.cfg.max_checks])) if base else (x.cfg.max_checks, 0)
    proposed = [c for c in obj.get("checks") or [] if isinstance(c, dict)]
    raw_claims = [cc for cc in obj.get("central_claims") or [] if isinstance(cc, dict)]
    claims_in = raw_claims[:MAX_CLAIMS]
    ks = (x.sealed("claims") or {}).get("claims") or []
    extracted: dict = {}
    if ks:
        # The main claims were extracted before this plan: the plan says how each is tested, by id, and cannot add, drop,
        # requote or retype one. Every claim has an entry in the first round; a follow-up round lists only those it takes
        # up, and may add a headline claim the extraction missed (`new_claims`, validated as the extractor's are).
        extracted = {k["id"]: k for k in ks}
        given = {str(cc.get("id") or ""): cc for cc in raw_claims}
        if (unknown := [i for i in given if i not in extracted]):
            errors.append(f"central_claims ids {unknown[:6]} are not extracted claims ({', '.join(extracted)}): a plan tests the "
                          "extracted claims by id; it never adds, requotes or retypes one"
                          + (" (a headline claim the extraction missed goes in `new_claims`)" if base else ""))
        want = [i for i in extracted if i in given] if base else list(extracted)
        if not base and (missing := [i for i in want if i not in given]):
            errors.append(f"main claim(s) {missing} have no entry: link the checks that test each, or give `why_unchecked` "
                          "and `blocker`")
        if base:
            known = ks + [c for p in [base] + report.withdrawn_plans(x.root) for c in p.get("central_claims", [])
                          if c.get("id") and c["id"] not in extracted]
            top = max([int(m.group(1)) for c in known if (m := re.fullmatch(r"K(\d+)", str(c.get("id") or "")))] + [0])
            raw_new = [c for c in obj.get("new_claims") or [] if isinstance(c, dict)][:6]   # ponytail: 6 new claims per follow-up
            new, ndrop, nerr = _validate_claims(x, raw_new, known, top + 1)
            errors += nerr
            dropped += [{"check": "new claim", "why": d["why"]} for d in ndrop]
            for n in new:
                extracted[n["id"]] = {**{k: v for k, v in n.items() if k != "_raw"}, "origin": "plan:2"}
                given[n["id"]] = raw_new[n["_raw"]]
                want.append(n["id"])
        claims_in = [{**given.get(i, {"checks": [], "why_unchecked": "the plan gave no entry for this claim"}),
                      **{k: extracted[i][k] for k in ("quote", "claim_type", "scope")}, "id": i} for i in want]
    elif len(raw_claims) > MAX_CLAIMS:                        # refused, never cut unseen; on the last attempt, recorded
        errors.append(f"{len(raw_claims)} central claims: at most {MAX_CLAIMS} — keep the paper's most important ones and "
                      "merge restatements of one claim; a claim past the cap is recorded as dropped, never checked")
        dropped += [{"check": "central claim", "why": f"over the cap of {MAX_CLAIMS} central claims: {str(cc.get('quote'))[:160]!r}"}
                    for cc in raw_claims[MAX_CLAIMS:]]
    # A follow-up claim may rest on a check an earlier round ran (its harness id, C1..): its covers count for the claim
    # (Oct-01 PPRM: "already run in round 1 as C3" could only be written as an omission).
    proposed_ids = {c.get("id") for c in proposed}
    earlier = {c["id"]: c for c in (base or {}).get("checks", []) if c["id"] not in proposed_ids}
    # Central claims get the budget first, and within them the checks that run the requested experiment: a
    # check no (re-found) central claim cites is incidental, is cut before any central one, and must say why
    # no central claim could use its slot; a supporting check is cut before a target one.
    central_ids = {k for cc in claims_in if x.paper.find(str(cc.get("quote") or ""))[0] for k in cc.get("checks") or []}
    proposed.sort(key=lambda c: (c.get("id") not in central_ids, c.get("role") != "target"))
    for c in proposed[cap:]:
        dropped.append({"check": c.get("id"), "why": f"over the budget of {cap} checks"})
    # What a check cut by the check budget would have covered is uncovered because of a configured cap, said by the harness
    # (Oct-02: it read `unstated`, the planner's); the caps this round reached are the facts a `cap` blocker may rest on.
    cut = {flat(s): str(c.get("id")) for c in proposed[cap:] for s in c.get("covers") or []}
    setting = "SH_MAX_FOLLOWUP_CHECKS" if base else "SH_MAX_CHECKS"
    caps_hit = tuple(([f"SH_MAX_DISCOVERIES: {spent} of {x.cfg.max_discoveries} searches spent"] if spent >= x.cfg.max_discoveries
                      else []) + ([f"{setting}: {len(proposed)} checks proposed for {cap} slots"] if len(proposed) >= cap else []))
    for i, c in enumerate(proposed[:cap], n0 + 1):
        cid, kind, errs = f"C{i}", c.get("kind"), []
        if kind not in KINDS:
            errs.append(f"{cid}: unknown kind {kind!r}")
        claim = _find(x, c.get("claim_quote", ""), errs, f"{cid} claim_quote")
        role, criterion = _enum(c.get("role"), ROLES), _enum(c.get("criterion"), CRITERIA)
        if not role:
            errs.append(f"{cid}: `role` is 'target' (it runs the experiment the claim names, on what the claim names) or "
                        "'supporting' (it stands beside it: a simulation, a stand-in dataset or model, a related lemma)")
        if not criterion and kind != "CERTIFICATE":
            errs.append(f"{cid}: `criterion` is 'stated' (the claim's own sentence states the comparison or number this "
                        "target encodes) or 'supplied' (it states neither, and you chose the relation, rival, baseline or "
                        "threshold)")
        rec = {"id": cid, "proposed_id": c.get("id"), "kind": kind, "concerns": [k for k in c.get("concerns") or []
                                                                               if k in concern_ids],
               "claim": claim["quote"] if claim else "", "why": str(c.get("why") or "")[:1000],
               "metric": str(c.get("metric") or "").strip(), "repo_attributed": attributed,
               "central": c.get("id") in central_ids, "incidental_why": str(c.get("incidental_why") or "")[:600],
               # a certificate checks the printed statement itself: there is no criterion to choose
               "role": role or "supporting", "criterion": "stated" if kind == "CERTIFICATE" else criterion or "supplied",
               # ponytail: 24 covered items per check; an entry listing several is several (report.parts)
               "covers": [p[:120] for s in c.get("covers") or [] for p in _parts(str(s))][:24],
               "acquire": _acquire(x, c, errs, cid) if kind in SCRIPT_KINDS else []}
        rec["basis"] = BASIS.get(kind, "")
        if kind == "RELEASED_DATA":
            rec["basis"] = str(c.get("basis") or "")
            if rec["basis"] not in RELEASED_BASES:
                errs.append(f"{cid}: RELEASED_DATA needs `basis`: 'published_results' (an audit of the released result "
                            "files the printed numbers were reported from) or 'predictions' (the metric recomputed from "
                            "released per-item predictions or scores)")
            if not rec["acquire"] and not state.read_json(x.root / "released.json", []):
                errs.append(f"{cid}: RELEASED_DATA recomputes from files the authors released, but the checkout releases no "
                            "data files and this check acquires none: `acquire` the record the paper cites (or one a "
                            "registry search returned), or run the experiment as a RECONSTRUCTION")
        if kind == "RECONSTRUCTION":
            rec["test"] = _enum(c.get("test"), ("performance", "compatibility"))
            if not rec["test"]:
                errs.append(f"{cid}: `test` is 'performance' (a comparison over replicates) or 'compatibility' (an "
                            "engineering claim: the component integrates and trains)")
            if rec["test"] == "compatibility" and not (c.get("target") or {}).get("relation"):
                errs.append(f"{cid}: a compatibility test states its condition as a relation target (e.g. "
                            "\"loss_first - loss_last > 0\")")
        if isinstance(c.get("define"), dict):
            rec["define"] = {str(k)[:60]: str(v)[:300] for k, v in list(c["define"].items())[:12]}
        if c.get("readings"):
            if kind == "CERTIFICATE":
                errs.append(f"{cid}: a CERTIFICATE states another reading of its statement through its deviations and "
                            "`literal` (the text as printed); `readings` are two definitions of a computed quantity")
            rec["readings"] = _readings(x, c["readings"], errs, cid)
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
    by_pid = {k["proposed_id"]: k for k in checks}
    by_pid.update({k: c for k, c in earlier.items()})
    # Every claim an earlier round sealed — the first plan's, and a withdrawn follow-up's — keeps its type; a quote inside
    # one of them (or around it) is that claim.
    prior = [(b["quote"], b.get("claim_type", "")) for p in [base or {}] + (report.withdrawn_plans(x.root) if base else [])
             for b in p.get("central_claims", [])]
    for cc in claims_in:
        h = _find(x, str(cc.get("quote") or ""), errors, "central claim")
        if not h:
            dropped.append({"check": "central claim", "why": f"quote not found: {str(cc.get('quote'))[:120]!r}"})
            continue
        cerrs: list[str] = []
        label = f"central claim {h['quote'][:50]!r}"
        # A claim's type decides which rules guard it, so it is never left out and never changes between rounds.
        ctype = _enum(cc.get("claim_type"), CLAIM_TYPES)
        was = next((t for q, t in prior if t and report.same_claim(q, h["quote"])), "")
        if not ctype:
            cerrs.append(f"{label}: `claim_type` is one of {list(CLAIM_TYPES)}")
        elif was and was != ctype:
            cerrs.append(f"{label}: an earlier round typed it {was!r} (the same claim, or a quote inside it); a follow-up "
                         "round never retypes a claim")
            ctype = was
        # Scope: every method, dataset or setting the claim names is covered by one of its TARGET checks or
        # omitted with a reason — a narrower test, or a supporting stand-in, never silently stands for the claim.
        scope = [str(s)[:120] for s in cc.get("scope") or [] if str(s).strip()]
        if len(scope) > MAX_SCOPE:              # refused, never cut unseen (an extracted claim is already within it)
            cerrs.append(f"{label}: {len(scope)} scope items: at most {MAX_SCOPE}")
        scope = scope[:MAX_SCOPE]
        if listed := [(s, p) for s in scope if len(p := _parts(s)) > 1]:
            cerrs.append(f"{label}: scope item(s) {[s for s, _ in listed][:4]} each name several items ({[p for _, p in listed][:2]}): "
                         "give each dataset, model, method or setting its own scope item, covered by a check or omitted with "
                         "its blocker (one entry for five benchmarks read as covered while one never arrived)")
            scope = [q for s in scope for q in _parts(s)][:MAX_SCOPE]     # sealed on the last attempt: each its own item
        if not scope:
            cerrs.append(f"{label}: `scope` lists every method, dataset, setting and metric the claim names (the statement "
                         "itself, for a theorem)")
        omitted = [{"item": str(o.get("item") or "")[:120], "why": str(o.get("why") or "")[:400],
                    "blocker": str(o.get("blocker") or "")[:20], "discovery": [str(d)[:12] for d in o.get("discovery") or []][:6],
                    "failed_checks": [str(d)[:12] for d in o.get("failed_checks") or []][:6],
                    "artifact": str(o.get("artifact") or "")[:200], "service": o.get("service") is True,
                    "paper_quote": str(o.get("paper_quote") or "")[:400],
                    "not_the_dataset": str(o.get("not_the_dataset") or "")[:600]}
                   for o in cc.get("omitted") or [] if isinstance(o, dict)]
        links = [k for k in cc.get("checks") or [] if k in proposed_ids or k in earlier]
        # A check stands for this claim only if its kind can: an empirical claim's target runs the experiment
        # (never a certificate, nor the paper's own arithmetic except for a printed value); an engineering claim's
        # target is a compatibility test; a compatibility test speaks for nothing else.
        for key in [i for i in links if i in by_pid]:
            k, bad = by_pid[key], ""
            if k["role"] == "target" and ctype != "theory" and (     # untyped: the strictest reading, an experiment
                    k["kind"] == "CERTIFICATE" or (k["kind"] == "ARITHMETIC" and ctype != "value")):
                bad = (f"check {key} is a {k['kind']} (exact instances / the paper's own numbers); it cannot stand "
                       f"for the {ctype or 'untyped (so: empirical)'} claim {h['quote'][:50]!r}, which is about an experiment: give "
                       "it role 'supporting', or link the check that runs the experiment")
            elif k["role"] == "target" and ctype == "engineering" and k.get("test") != "compatibility":
                bad = (f"{label} is an engineering claim: its target check {key} must be a RECONSTRUCTION "
                       "compatibility test (a performance comparison is a separate claim, with its own quote)")
            elif k.get("test") == "compatibility" and ctype != "engineering":
                bad = (f"check {key} is a compatibility test: it may speak only for a claim whose claim_type is "
                       "'engineering'")
            if bad:
                cerrs.append(bad)
                links.remove(key)                     # sealed on the last attempt: unlinked, never counted
        have = set().union(*(set(map(flat, by_pid[k]["covers"])) for k in links if k in by_pid
                             and by_pid[k]["role"] == "target")) | {flat(o["item"]) for o in omitted if o["why"]}
        if links and (miss := [s for s in scope if flat(s) not in have]):
            by_cut = {s: cut[flat(s)] for s in miss if flat(s) in cut}
            cerrs.append(f"{label}: scope item(s) {miss} are neither in a linked TARGET check's `covers` nor in `omitted` with "
                         "a reason (a supporting check stands beside the claim and covers nothing of it)" + "".join(
                             f"; {s!r} is covered only by {k}, which is past the check budget ({setting}={cap}): drop a "
                             "lower-priority check, or omit the item with blocker `cap`" for s, k in by_cut.items()))
            omitted += [{"item": s, "why": f"its check {by_cut[s]} was cut by the check budget ({setting}={cap}), a setting of "
                                           "this run" if s in by_cut else "neither covered by a target check nor omitted with "
                                                                         "a reason in the sealed plan",
                         "blocker": "cap" if s in by_cut else "unstated", "basis": "harness" if s in by_cut else "",
                         "discovery": [], "failed_checks": [], "artifact": "", "service": False,
                         "paper_quote": "", "not_the_dataset": ""} for s in miss]
        # What is not run says what stops it, for every claim type, checked against the harness's own records.
        claim_blocker = {k: cc.get(k) for k in ("blocker", "discovery", "failed_checks", "not_the_dataset", "artifact",
                                                "service", "paper_quote")}
        for o in [o for o in omitted if o["why"] and o["blocker"] != "unstated" and o.get("basis") != "harness"]:
            if (why := _blocker(x, o, cerrs, f"{label} omitted {o['item']!r}", caps_hit)):
                o["unverified"] = why[:400]
            if o["blocker"] in ("other", "protocol") and spent >= x.cfg.max_discoveries:
                o["budget_spent"] = f"{spent} of {x.cfg.max_discoveries}"   # a fact beside the planner's word (Oct-01)
        if not links and (why := _blocker(x, claim_blocker, cerrs, f"{label} has no check; why_unchecked", caps_hit)):
            claim_blocker["unverified"] = why[:400]
        if not links and claim_blocker["blocker"] in ("other", "protocol") and spent >= x.cfg.max_discoveries:
            claim_blocker["budget_spent"] = f"{spent} of {x.cfg.max_discoveries}"   # a whole claim given up too (Oct-01)
        errors += cerrs
        central.append({"quote": h["quote"], "page": h["page"], "checks": links, "scope": scope, "omitted": omitted,
                        "claim_type": ctype, "why_unchecked": str(cc.get("why_unchecked") or "")[:600],
                        "blocker": str(claim_blocker["blocker"] or "")[:20],
                        "discovery": [str(d)[:12] for d in claim_blocker["discovery"] or []][:6],
                        "failed_checks": [str(d)[:12] for d in claim_blocker["failed_checks"] or []][:6],
                        "not_the_dataset": str(claim_blocker["not_the_dataset"] or "")[:600],
                        **({"unverified": claim_blocker["unverified"]} if claim_blocker.get("unverified") else {}),
                        **({"budget_spent": claim_blocker["budget_spent"]} if claim_blocker.get("budget_spent") else {}),
                        **({"cap": claim_blocker["cap"]} if claim_blocker.get("cap") else {}),
                        # the extracted claim's own record: its id, plain statement, assumptions, what would decide it
                        **({k: extracted[cc["id"]][k] for k in ("id", "statement", "assumptions", "required_evidence",
                                                                "interpretations", "also_stated", "evidence_in_paper", "origin")
                            if k in extracted[cc["id"]]} if cc.get("id") in extracted else {}),
                        # sealed on the last attempt with these problems: each was resolved to its strictest reading
                        **({"sealed_with_errors": cerrs[:8]} if cerrs else {})})
    _fail_or_drop(errors, final)
    # The planner's own ids map onto the harness's C1..Cn; a check whose only claim link was removed is incidental.
    ids = {c["proposed_id"]: c["id"] for c in checks}
    for cc in central:
        cc["checks"] = [ids.get(k, k) for k in cc["checks"] if k in ids or k in earlier]
    linked = {k for cc in central for k in cc["checks"]}
    for c in checks:
        if c["central"] and c["id"] not in linked:
            c["central"], c["incidental_why"] = False, c["incidental_why"] or "its link to a central claim was refused at the seal"
    return {"checks": checks, "central_claims": central, "dropped": dropped,
            "repo_is_authors": attributed, "repo_note": str(obj.get("repo_note") or "")[:1000]}


def _readings(x: _Ctx, items, errs: list[str], cid: str, record: Path | None = None) -> list[dict]:
    """Two or three definitions of one compared quantity, each re-found where it is stated: in the
    paper (verbatim), in a tracked checkout file (the literal code lines), or — for the script author,
    once the check's data is acquired — in a file of the released record kept as quote-only text
    (`record:<n>/<path>`: its code, notebook or README, never mounted and never run). Two readings with
    the same words are one definition."""
    out, given = [], [r for r in items or [] if isinstance(r, dict)]
    if len(given) > 3:                                                      # refused by name, never cut unseen
        errs.append(f"{cid}: {len(given)} readings: at most 3 (the paper's definition, the code's, a released record's). "
                    "Put every printed choice in ONE reading with source 'paper' and every changed one in ONE other reading, "
                    "and tie each such deviation to that reading; readings "
                    f"{[str(r.get('name')) for r in given[3:]]} are not kept")
    for r in given[:3]:                                                     # ponytail: 3 readings per check
        name, src, quote = str(r.get("name") or ""), str(r.get("source") or "").strip(), str(r.get("quote") or "")
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,23}", name) or name in {o["name"] for o in out}:
            errs.append(f"{cid}: reading name {name!r} must be a distinct short lowercase identifier")
            continue
        if flat(quote) and flat(quote) in {flat(o["quote"]) for o in out}:
            errs.append(f"{cid}: readings {name!r} and another quote the same words: one definition, not two")
            continue
        if src == "paper":
            h = _find(x, quote, errs, f"{cid} reading {name}")
            if h:
                out.append({"name": name, "source": "paper", "quote": h["quote"], "page": h["page"]})
            continue
        if not _code_quote(x, src, quote, record):
            errs.append(f"{cid}: reading {name}: the quote is not literal text (20+ chars) of the released record's file "
                        f"{src!r} (the files listed under record_src in the acquired-data manifest)" if src.startswith("record:")
                        else f"{cid}: reading {name}: the quote is not literal code (20+ chars) of tracked file {src!r}")
            continue
        out.append({"name": name, "source": src, "quote": quote[:1500]})
    if items and len(out) < 2:
        errs.append(f"{cid}: `readings` needs at least two definitions that re-find (the paper's and the code's)")
    return out


def _code_quote(x: _Ctx, src: str, quote: str, record: Path | None = None) -> bool:
    """Is `quote` literal text (20+ characters) of a file git tracks in the pinned checkout, or of a released record's
    file kept as quote-only text (`record:<path>`, unchanged since it was acquired)?"""
    src = str(src or "").strip()
    if src.startswith("record:"):
        rel = src[len("record:"):].strip().replace("\\", "/").removeprefix("./")
        listed = {r.get("path"): r.get("sha256") for r in (state.read_json(record.parent / "data.json") or {}).get(
            "record_src") or []} if record is not None else {}
        f = record / rel if record is not None and rel in listed and ".." not in rel.split("/") else None
        if f and f.is_file() and state.sha256(f.read_bytes()) != listed[rel]:
            f = None                                     # changed since the acquisition kept it
    else:
        f = _repo_file(x, src)
    return bool(f and f.is_file() and len(flat(quote)) >= 20 and flat(quote) in flat(f.read_text(encoding="utf-8",
                                                                                                    errors="replace")))


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
    c = next(k for k in x.plan()["checks"] if k["id"] == cid)
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
    c = next(k for k in x.plan()["checks"] if k["id"] == cid)
    script = str(obj.get("script") or "")
    need = REQUIRED[c["kind"]]
    # Any number of bindings per kind (a method with two ingredients binds both); each kind needs one.
    given = [b for b in obj.get("bindings") or [] if isinstance(b, dict) and b.get("kind") in need][:12]  # ponytail: 12
    na = {b["kind"]: str(b["not_applicable"]).strip()[:500] for b in given
          if b["kind"] in OPTIONAL.get(c["kind"], ()) and len(str(b.get("not_applicable") or "").strip()) >= 20}
    if not script.strip() or any(not any(b.get("kind") == k and b.get("impl_quote") for b in given) and k not in na for k in need):
        return {"refused": True, "notes": str(obj.get("notes") or "the script or a required binding is missing")[:2000]}
    errors, out = [], []
    rel = [f["path"] for f in state.read_json(x.root / "released.json", [])] + [   # released, or acquired for it
        f"{execute.DATA_MOUNT}/{f['path']}" for f in (state.read_json(x.root / "checks" / cid / "data.json") or {}).get(
            "files", [])]
    for b in given:
        k = b["kind"]
        if k in na and not b.get("impl_quote"):
            out.append({"kind": k, "impl_quote": "", "paper_quote": "", "page": None, "not_applicable": na[k]})
            continue
        if flat(str(b.get("impl_quote") or "")) not in flat(script):
            errors.append(f"binding {k}: impl_quote is not in the script")
        h = None if k == "instance" else _find(x, b.get("paper_quote", ""), errors, f"binding {k} paper_quote")
        out.append({"kind": k, "impl_quote": str(b.get("impl_quote") or "")[:1000], "paper_quote": h["quote"] if h else "",
                    "page": h["page"] if h else None})
    of = lambda k: [b for b in out if b["kind"] == k]
    if c["kind"] == "RELEASED_DATA" and not any(p in b["impl_quote"] and p in script for b in of("dataset") for p in rel):
        errors.append("the dataset binding must open a released file by its full relative path")
    if c["kind"] == "CERTIFICATE" and c.get("step") and not any(flat(c["step"]) in flat(b["paper_quote"])
                                                                for b in of("claimed_bound")):
        errors.append("claimed_bound must be the proof step the harness bound")
    try:
        execute.packages(script)
    except ValueError as e:
        errors.append(str(e))
    runs = obj.get("runs") if isinstance(obj.get("runs"), int) and not isinstance(obj.get("runs"), bool) else 1
    cap = 200 if c["kind"] == "CERTIFICATE" else x.cfg.max_runs     # ponytail: 200 sandboxed runs per check
    if not 1 <= runs <= cap:
        if final or c["kind"] != "CERTIFICATE":         # a stated run count past the cap: refused, never shrunk
            return {"refused": True, "by": "harness", "notes": f"{runs} runs is outside 1..{cap}: refused rather than downscaled"}
        errors.append(f"`runs` is at most {cap}: each run is one sandboxed process. A certificate may check several "
                      "instances per run by printing one REFEREE_RESULT line per instance")
    stochastic = obj.get("stochastic")
    if c["kind"] == "RECONSTRUCTION" and c.get("test") != "compatibility" and not isinstance(stochastic, bool):
        errors.append("`stochastic` must be true (the seed drives randomness: training, sampling, simulation) or false "
                      "(the computation is deterministic: the harness runs it twice and requires identical results)")
    if runs > 1 and c["kind"] == "RECONSTRUCTION" and stochastic is True and c.get("test") != "compatibility" and not (
            (h := x.paper.find(str(obj.get("runs_quote") or ""))[0]) and value_in(h["quote"], str(runs))):
        errors.append("runs > 1 needs runs_quote: the paper's sentence printing that number, verbatim")
    flow = independence.seed_flow(script) if c["kind"] == "RECONSTRUCTION" else None
    if c["kind"] == "RECONSTRUCTION" and c.get("test") != "compatibility" and stochastic is True and flow is False:
        errors.append("`stochastic` is true, but `--seed` never reaches a random generator in the script (no default_rng, "
                      "RandomState, seed or manual_seed call, no random_state=, is fed from it): the replicates would be "
                      "one run repeated. Seed every generator from --seed, or declare `stochastic`: false")
    if c["kind"] == "RECONSTRUCTION" and c.get("test") != "compatibility" and stochastic is True and not re.search(
            r"""["']data_fingerprint["']""", script):
        errors.append("a stochastic script prints `data_fingerprint` on every result line: a sha256 of the random draws that "
                      "stage consumed (sampled indices, generated data, initial weights) — how the harness tells replicates "
                      "that differ from one run repeated")
    if c.get("acquire") and c["kind"] in ("RELEASED_DATA", "RECONSTRUCTION") and execute.DATA_MOUNT not in script:
        errors.append(f"this check acquired data for the claim (see the manifest), but the script never reads {execute.DATA_MOUNT}: "
                      "a simulation or surrogate in its place is a different experiment. Read the acquired files; a simulation "
                      "belongs in a separate check with role 'supporting'")
    if c["kind"] == "CERTIFICATE" and obj.get("readings"):
        errors.append("a CERTIFICATE states another reading of its statement through its deviations and `literal` (the text "
                      "as printed); `readings` are two definitions of a computed quantity")
    readings = _readings(x, obj.get("readings"), errors, cid, x.root / "checks" / cid / "record_src") \
        if obj.get("readings") and c["kind"] != "CERTIFICATE" else []
    if (c.get("readings") or readings) and not all(re.search(rf"""["']{k}["']""", script) for k in ("reading", "cohort")):
        errors.append("with readings, each result line carries `reading` (its name) and `cohort` (the items it was "
                      "computed over), every reading in the same run")
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
    deviations, given_devs = [], [d for d in obj.get("deviations") or [] if isinstance(d, dict)]
    if len(given_devs) > MAX_DEVIATIONS:
        errors.append(f"{len(given_devs)} deviations: at most {MAX_DEVIATIONS} — merge choices of one kind (several "
                      "unstated hyperparameters, several open orderings) into one entry with all of them in `used`; none "
                      "is ever dropped unseen")
    for d in given_devs[:MAX_DEVIATIONS]:
        h = _find(x, str(d["printed"]), errors, "deviation `printed`") if d.get("printed") else None
        if not str(d.get("used") or "").strip():
            errors.append("each deviation needs `used`: what the script does instead of the printed text")
        if not isinstance(d.get("changes_claim"), bool):
            errors.append("each deviation needs `changes_claim`: true if it changes what the printed claim says (a "
                          "premise dropped or added, the conclusion, an index or a definition), false if it only fixes "
                          "a detail the claim leaves open")
        deviations.append({"printed": h["quote"] if h else "", "page": h["page"] if h else None,
                           "used": str(d.get("used") or "")[:600], "why": str(d.get("why") or "")[:600],
                           "changes_claim": d.get("changes_claim") is True})
    names = {rd["name"] for rd in (c.get("readings") or []) + readings}
    declared = {str(rd.get("name")) for rd in obj.get("readings") or [] if isinstance(rd, dict)}
    paper_names = {rd["name"] for rd in (c.get("readings") or []) + readings if rd.get("source") == "paper"}
    has_paper = bool(paper_names)
    for i, (d, dev) in enumerate(zip(given_devs[:MAX_DEVIATIONS], deviations)):
        rd, why_not = str(d.get("reading") or "").strip()[:24], str(d.get("printed_infeasible") or "").strip()[:400]
        dev.update(**({"reading": rd} if rd else {}), **({"printed_infeasible": why_not} if why_not else {}))
        # Readings are never chosen (invariant 15): a script that departs from a printed definition where the claim is
        # decided also computes the printed one, beside it, in the same run (Sep-29 label ranking C9: the paper's r/sum r
        # was replaced by a softmax and the printed estimator was never decided), or says why the printed text cannot run.
        if c["kind"] in ("RELEASED_DATA", "RECONSTRUCTION") and dev["changes_claim"] and dev["printed"]:
            if rd and rd not in names and rd in declared:
                errors.append(f"deviation {i} names reading {rd!r}, which you declared but which was not kept (the "
                              "`readings` error above says why)")
            elif rd and rd not in names:
                errors.append(f"deviation {i} names reading {rd!r}, which the check does not declare in `readings`")
            elif rd and rd in paper_names:
                errors.append(f"deviation {i} names reading {rd!r}, the printed definition itself: a change holds in the OTHER "
                              "reading (name that one); the paper's reading computes the printed text unchanged")
            elif rd and not has_paper:
                errors.append(f"deviation {i} is tied to reading {rd!r}: the printed definition is computed beside it, as a "
                              "reading with source 'paper' (its words verbatim), in the same run and cohort")
            elif not rd and len(why_not) < 20:
                errors.append(f"deviation {i} changes the printed text ({dev['printed'][:80]!r}): compute the printed version "
                              "too — a reading with source 'paper', and this deviation's `reading` naming the other one — or "
                              "say in `printed_infeasible` why the printed text cannot be computed (a detail it never states)")
    fidelity = _fidelity(x, c, obj.get("fidelity"), names, deviations, errors) if c["kind"] in (
        "RELEASED_DATA", "RECONSTRUCTION") else []
    revisions = _revision(x, cid, int(r), runs, deviations, obj, errors, final)
    if c.get("criterion") == "supplied":   # a qualitative claim tested by a criterion the planner chose: its result is about that criterion
        deviations.append(_supplied(c))
    if c["kind"] == "CERTIFICATE":   # a counterexample must satisfy every premise of the exact claim
        if not re.search(r"""["']premises_hold["']""", script):
            errors.append("each result line reports `premises_hold`: 1 if every premise of the claim being tested holds "
                          "on the instance (evaluated exactly), else 0 — never drop a premise to get a violation")
        if any(d["changes_claim"] for d in deviations) and not re.search(r"""["']literal["']""", script):
            errors.append("a certificate that changes the claim's reading also reports `literal` (holds, fails, "
                          "undefined or premise_not_met) for the text exactly as printed")
    if errors and final:                  # the harness's refusal of the answer, never the author's refusal of the check
        return {"refused": True, "by": "harness", "notes": "; ".join(errors)[:2000]}
    _fail_or_drop(errors, final)
    cdir = x.root / "checks" / cid
    cdir.mkdir(parents=True, exist_ok=True)
    (cdir / f"script.{r}.py").write_bytes(script.encode("utf-8"))   # bytes: the sha is of exactly these
    return {"script_sha256": state.sha256(script), "runs": runs, "runs_quote": str(obj.get("runs_quote") or "")[:500],
            "metric": metric, "outputs": outputs, "deviations": deviations, "bindings": out,
            "stochastic": stochastic if isinstance(stochastic, bool) else None, "seed_flow": flow, "readings": readings,
            **({"revisions": revisions} if revisions else {}), **({"fidelity": fidelity} if fidelity else {}),
            "checked_statement": "proof_step" if c.get("step") else str(obj.get("checked_statement") or "conclusion"),
            # A general argument (e.g. that a printed premise can never hold) is the author's reasoning:
            # shown to the verifier and the reader as such, never counted as an executed result.
            "premise_argument": str(obj.get("premise_argument") or "")[:2000] if c["kind"] == "CERTIFICATE" else "",
            "notes": str(obj.get("notes") or "")[:2000]}


FIDELITY = ("data", "model", "metric", "baselines", "preprocessing", "sample_size", "statistics")


def _fidelity(x: _Ctx, c: dict, items, names: set, deviations: list[dict], errors: list[str]) -> list[dict]:
    """The script author's account of each aspect of the experiment against the paper and, where they ship it, the
    authors' code or the released record: the dataset, the model, the metric, the baselines, the preprocessing, the
    sample sizes and the statistical rule — what the paper says (verbatim), what the code does (its literal lines), what
    the script uses. Every aspect is stated (or `not_applicable` with why). Where paper and code disagree the script never
    picks one silently: on the compared quantity (`metric`) both are computed as readings; on any other aspect it
    computes both (a reading) or explains the disagreement, and the report shows it beside the result."""
    out, seen = [], set()
    record = x.root / "checks" / c["id"] / "record_src"
    for f in [f for f in items or [] if isinstance(f, dict)][:14]:
        a = str(f.get("aspect") or "")
        if a not in FIDELITY or a in seen:
            errors.append(f"fidelity: aspect {a!r} is one of {list(FIDELITY)}, each once")
            continue
        seen.add(a)
        if len(na := str(f.get("not_applicable") or "").strip()) >= 10:
            out.append({"aspect": a, "not_applicable": na[:300]})
            continue
        pq = str(f.get("paper") or "").strip()
        h = _find(x, pq, errors, f"fidelity {a}: `paper`") if pq else None
        code = f.get("code") if isinstance(f.get("code"), dict) else {}
        src, cq = str(code.get("file") or "").strip(), str(code.get("quote") or "")
        if src and not _code_quote(x, src, cq, record):
            errors.append(f"fidelity {a}: `code` quote is not literal text (20+ chars) of {src!r} (a tracked checkout file, or "
                          "record:<path> of the released record)")
            src = ""
        used = str(f.get("used") or "").strip()
        if len(used) < 5:
            errors.append(f"fidelity {a}: `used` says what the script does")
        agrees = f.get("agrees")
        rd, expl = str(f.get("reading") or "").strip()[:24], str(f.get("explained") or "").strip()[:600]
        if pq and src and not isinstance(agrees, bool):
            errors.append(f"fidelity {a}: with the paper's words and the code's lines both given, `agrees` is true or false")
        if agrees is False:
            if a == "metric" and not (rd and rd in names):
                errors.append("fidelity metric: the paper and the code define the compared quantity differently — compute both "
                              "as `readings` (same data, same cohort) and name the code's reading in `reading`; never choose one")
            elif a != "metric" and not (rd and rd in names) and len(expl) < 20:
                errors.append(f"fidelity {a}: the paper and the code disagree — compute both (a reading), or say in "
                              "`explained` what differs, which one the script follows and why")
        if not pq and not src and not any(not d["printed"] for d in deviations):
            errors.append(f"fidelity {a}: neither the paper's words nor the code's lines are given — if the paper is silent "
                          "here, the script's choice is a deviation with an empty `printed`")
        out.append({"aspect": a, "paper": h["quote"] if h else "", **({"page": h["page"]} if h else {}),
                    "code": {"file": src, "quote": cq[:600]} if src else None, "used": used[:600],
                    "agrees": agrees if isinstance(agrees, bool) else None, **({"reading": rd} if rd else {}),
                    **({"explained": expl} if expl else {})})
    if (miss := [a for a in FIDELITY if a not in seen]):
        errors.append(f"fidelity: state every aspect {miss} against the paper and the released code (or `not_applicable` "
                      "with why)")
    return out


def _revision(x: _Ctx, cid: str, r: int, runs: int, deviations: list[dict], obj: dict, errors: list[str], final: bool) -> dict:
    """Self-correction keeps the scope: a revised script that drops (or relabels) a claim-changing deviation the previous
    round declared, or runs fewer instances or seeds, says in `revision_notes` what in the script changed — the verifier
    sees it, and the ledger keeps it. Unexplained on the last attempt, the dropped deviation is carried forward (the
    strictest reading), never lost (Oct-01 conformal C5: 1 claim-changing deviation in round 1, none by round 3)."""
    prev = x.sealed(f"gen:{cid}.{r - 1}") if r > 1 else None
    if not prev:
        return {}
    keep = [d for d in deviations if d["changes_claim"]]
    same = lambda a, b: (a.get("printed") and flat(a["printed"]) == flat(b.get("printed") or "")) or flat(a["used"]) == flat(b["used"])
    gone = [d for d in prev.get("deviations") or [] if d.get("changes_claim") and not d.get("changes_claim_by")
            and "supplied the decision criterion" not in d.get("used", "") and not any(same(d, k) for k in keep)]
    notes = [{"was": str(n.get("was") or "")[:300], "why": str(n.get("why") or "").strip()[:600]}
             for n in obj.get("revision_notes") or [] if isinstance(n, dict)]
    told = lambda d: any(len(n["why"]) >= 20 and len(flat(n["was"])) >= 10 and (flat(n["was"]) in flat(d["used"]) or flat(d["used"])
                                                                                in flat(n["was"])) for n in notes)
    open_ = [d for d in gone if not told(d)]
    fewer = isinstance(prev.get("runs"), int) and runs < prev["runs"] and not any(flat(n["was"]) == "runs" and len(n["why"]) >= 20
                                                                                for n in notes)
    if (open_ or fewer) and not final:
        errors.append(f"round {r} drops or relabels the claim-changing deviation(s) round {r - 1} declared "
                      f"{[d['used'][:100] for d in open_]}" + (f" and runs {runs} instead of {prev['runs']}" if fewer else "")
                      + ": if the script no longer departs from the paper there, say in `revision_notes` "
                        "([{\"was\": the earlier deviation's `used` text (or \"runs\"), \"why\": what in the script changed}]) "
                        "— the verifier checks it; a revision never weakens a claim-changing choice silently")
    for d in open_ if final else []:                  # the last attempt: carried forward as it was, the strictest reading
        deviations.append({k: d.get(k) for k in ("printed", "page", "used", "why")} | {
            "changes_claim": True, "why": f"carried from round {r - 1}: dropped without a stated reason ({d.get('why', '')})"[:600]})
    return {"from_round": r - 1, "dropped_claim_changing": [d["used"] for d in gone if told(d)], "notes": notes} if (
        gone or notes or fewer) else {}


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
    c = next((k for k in (x.plan() or {}).get("checks", []) if k["id"] == cid), {})
    # Two keys on what decides which rule applies (invariant 7's pattern): the verifier states, independently of planner and
    # author, whether the claim's sentence states the compared relation, and which deviations change what is compared. Either
    # key alone moves the result to the changed claim; neither key ever moves it back (Oct-01 transformer C7, conformal C4/C6).
    keys = {}
    if verdict == "APPROVE":
        n = len(g.get("deviations") or [])
        crit = _enum(obj.get("criterion"), CRITERIA)
        chg = obj.get("claim_changing")
        two = c.get("kind") in ("RELEASED_DATA", "RECONSTRUCTION", "AUTHOR_CODE") and bool(c.get("target"))
        errs = ([] if crit or not two else ["`criterion` is 'stated' (the claim's own sentence states the comparison or number "
                                            "this check's target encodes) or 'supplied' (it states neither: a rival, threshold, "
                                            "held-out test or grid someone chose)"])
        if not isinstance(chg, list):
            errs.append("`claim_changing` lists the indices (from 0) of every deviation that changes what is compared or what "
                        "the claim says, whatever its author marked — [] when none does")
        elif any(not isinstance(i, int) or isinstance(i, bool) or not 0 <= i < n for i in chg):
            errs.append(f"`claim_changing` holds deviation indices from 0 to {n - 1}")
        _fail_or_drop(errs, final)
        keys = {"criterion": crit or ("supplied" if two else ""),                   # the last attempt: the strictest reading
                "claim_changing": sorted({i for i in chg if isinstance(i, int) and not isinstance(i, bool) and 0 <= i < n})
                if isinstance(chg, list) and not errs else list(range(n))}
    return {"verdict": verdict, "required_changes": str(obj.get("required_changes") or "")[:3000],
            "notes": str(obj.get("notes") or "")[:2000], "quotes": quotes, "script_sha256": g.get("script_sha256", ""), **keys}


def _seal_audit(x: _Ctx, tid: str, obj: dict, final: bool) -> dict:
    """An auditor's word stands only on the paper's own words: STANDS cites the text that fixes what the failure rests on;
    DEPENDS names each open reading or supplied choice with the paper's words (or the declared deviation that supplied it)
    and the reading under which the claim may hold. On the last attempt, an objection or an approval the paper does not
    ground is no audit: UNRESOLVED, and the failure is not counted as one of the printed claim (fail closed)."""
    verdict = obj.get("verdict") if obj.get("verdict") in ("STANDS", "DEPENDS") else None
    if verdict is None:
        raise SealError("verdict must be STANDS or DEPENDS")
    errors: list[str] = []
    quotes = [h["quote"] for q in obj.get("quotes") or [] if (h := _find(x, str(q), errors, "audit quote"))]
    devs = (state.read_json(x.root / "checks" / tid.split(":", 1)[1] / "check.json") or {}).get("deviations") or []
    items = []
    for d in [d for d in obj.get("depends_on") or [] if isinstance(d, dict)][:8]:   # ponytail: 8 objections per failure
        printed, dev = str(d.get("printed") or ""), d.get("deviation")
        dev = dev if isinstance(dev, int) and not isinstance(dev, bool) and 0 <= dev < len(devs) else None
        h = _find(x, printed, errors, "depends_on `printed`") if printed else None
        if printed and not h:
            continue
        if not printed and dev is None:
            errors.append("a `depends_on` item without the paper's words names `deviation`: the index of the script's declared "
                          "deviation that supplied the choice")
            continue
        alt, was = str(d.get("alternative") or "").strip(), str(d.get("tested_as") or "").strip()
        if len(alt) < 10 or len(was) < 5:
            errors.append("each `depends_on` item says what the check assumed (`tested_as`) and the reading or choice under "
                          "which the claim may hold (`alternative`)")
            continue
        items.append({"printed": h["quote"] if h else "", "page": h["page"] if h else None, "deviation": dev,
                      "tested_as": was[:500], "alternative": alt[:500], "why": str(d.get("why") or "")[:800]})
    if verdict == "STANDS" and not quotes:
        errors.append("STANDS cites, in `quotes`, the paper text that fixes each premise and procedure the failure rests on")
    if verdict == "DEPENDS" and not items:
        errors.append("DEPENDS names at least one reading or choice the failure rests on, with the paper's words")
    _fail_or_drop(errors, final)
    if (verdict == "STANDS" and not quotes) or (verdict == "DEPENDS" and not items):
        verdict = "UNRESOLVED"
    return {"verdict": verdict, "depends_on": items if verdict == "DEPENDS" else [], "quotes": quotes,
            "notes": str(obj.get("notes") or "")[:2000]}


def _seal_vision(x: _Ctx, tid: str, obj: dict, final: bool) -> dict:
    return {"read": {str(i.get("id")): str(i.get("printed") or "")[:120] for i in obj.get("items") or []
                     if isinstance(i, dict)}, "notes": str(obj.get("notes") or "")[:1000]}


def _seal_report(x: _Ctx, tid: str, obj: dict, final: bool) -> dict:
    """The plain-language parts of the reviewer page (reviewer.render checks each before it is published)."""
    ids = {cc.get("id") for cc in (x.plan() or {}).get("central_claims", []) if cc.get("id")}
    claims = [{"id": str(e.get("id")), "explanation": str(e.get("explanation") or "")[:1200]}
              for e in obj.get("claims") or [] if isinstance(e, dict) and str(e.get("id")) in ids]
    if ids and (miss := sorted(ids - {c["id"] for c in claims})) and not final:
        raise SealError(f"every main claim needs an `explanation`: missing {miss}")
    return {"overview": str(obj.get("overview") or "")[:1400], "claims": claims,
            "terms": [{"term": str(t.get("term"))[:60], "definition": str(t.get("definition") or "")[:400]}
                      for t in obj.get("terms") or [] if isinstance(t, dict) and t.get("term")][:12],
            "open_questions": [str(q)[:400] for q in obj.get("open_questions") or [] if isinstance(q, str)][:5],
            # the trace (review.md) still shows one summary: the overview
            "summary_md": str(obj.get("summary_md") or obj.get("overview") or "")[:8000]}


VALIDATORS = {"lens": _seal_lens, "critic": _seal_critic, "claims": _seal_claims, "plan": _seal_plan, "bind": _seal_bind,
              "gen": _seal_gen, "verify": _seal_verify, "report": _seal_report, "vision": _seal_vision,
              "audit": _seal_audit, "compare": _seal_compare}
_EMPTY = {"lens": {"concerns": [], "dropped": []}, "critic": {"reviews": []}, "claims": {"claims": [], "dropped": []},
          "plan": {"checks": [], "central_claims": [], "dropped": [], "repo_is_authors": False, "repo_note": ""},
          "bind": {"identity": {"established": False, "reason": "malformed binding answer"}, "command": "",
                   "metric": "", "seed_flag": "", "runs": 1, "prepare": ""},
          "gen": {"refused": True, "by": "harness", "notes": "malformed answer"},
          "verify": {"verdict": "UNCHECKABLE", "required_changes": "", "notes": "malformed verifier answer",
                     "quotes": [], "script_sha256": ""},
          "report": {"summary_md": "", "overview": "", "claims": [], "terms": [], "open_questions": []},
          "compare": {"claims": [], "notes": "malformed answer"}, "vision": {"read": {}, "notes": "malformed answer"},
          "audit": {"verdict": "UNRESOLVED", "depends_on": [], "quotes": [], "notes": "malformed answer"}}


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
    plan = report.merged(_sealed(root, "plan"), _sealed(root, "plan:2")) or {"checks": []}
    res = execute.try_script(cfg, pid, cid, script, next((k for k in plan["checks"] if k["id"] == cid), {}))
    if not res.get("retry"):                     # a draft the harness did not run (the GPU was held) costs no try
        state.append_jsonl(cdir / "tries.jsonl", {"task": tid, "script_sha256": state.sha256(script), **res})
    return res
