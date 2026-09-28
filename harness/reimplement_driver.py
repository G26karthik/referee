"""The fourth role — writes a GOVERNED RECONSTRUCTION from a paper's own specification,
when `harness.reimplement.assess` has already established that the paper says enough.

WHY THIS EXISTS, and what it replaces. Before this module, the only channel for PATH B
was `stages.probe.write_reimplementation_prompt`: a brief written to disk, an implementer
(human or model) fills `runs/<pid>/spec.json` by hand, and `run.py accept` seals it under
the existing `driver` provenance — the SAME provenance a human-authored mechanism script
for a `FOCUSED_VALIDATION_EXPERIMENT` uses, with no check that a REIMPLEMENTATION
specifically stayed inside what the paper specified. The brief asked the implementer
nicely not to invent anything; nothing verified that they hadn't.

Decision 1 closes that gap with a NEW provenance, `reimpl_exec` (see `harness.provenance`),
admissible only when every REQUIRED ingredient the paper supplies is bound to both a paper
locator and a VERIFIED implementation locator — `conformance()` below computes that
binding, and it does not trust the delegate's own say-so: `impl_quote` is re-checked
against the script text this call actually returned, the same discipline
`claims.verify_evidence` applies to a lens's citation. A binding the delegate claimed but
that is not literally present in the returned script is not bound.

The manual channel (`write_reimplementation_prompt` / `run.py accept`, routing through
`driver`) is UNCHANGED and still exists for an operator who wants to review and hand-seal a
reconstruction themselves. What sits alongside it is no longer a CLI subprocess this
module spawned: `persist_brief` writes the generator's brief to a deterministic path under
`projects/<pid>/tasks/`, `harness/tasks.py` lists it as a `reimpl_gen` task for the
controlling session's own subagent to answer, and a SEPARATE subagent answers the
`reimpl_verify` task built from `_verification_brief` — the same "generator and an
independently attributed verifier" shape this module always required, now carried out by
two isolated subagent contexts instead of two CLI processes. It does not touch `driver`,
`repo_exec`, `backends.authorize`'s repo-execution branch, or `experiment_id` at all.

WRITING is not RUNNING. Nothing here executes a proposed script; it only produces and
verifies TEXT. Whether the SEALED result may ever actually run is a completely separate
question, decided by `backends.authorize`'s `reimpl_exec` branch and gated by
`SH_ALLOW_REIMPLEMENTATION_EXEC` plus the same isolation floor `SH_ALLOW_REPO_EXEC`
requires (`harness.isolation.sufficient_for_repo_exec`) — decision 10: a model-authored
script nobody reviewed is third-party code, and no capability that increases third-party
code execution lands without that boundary enforced.

`python -m harness.reimplement_driver` runs the self-check.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from . import delegation, sealing, state
from .reviewer_cli import envelope_provenance, unwrap_envelope
from .schema import ReimplementationBinding, ReimplementationConformance, ReimplementationReadiness
from .config import Config
from .prompts import reimplement as RP

# Every writer a validated reconstruction can carry: this module's own subprocess, a human
# `accept_reimplementation` call, plus every mode `harness.delegation` admits — the same
# vocabulary `verdict_driver.WRITERS` reads off, for the same reason: a mode sealed here
# must not be refused by `load_accepted`.
WRITERS = ("reimplement_driver",) + tuple(delegation.WRITTEN_BY.values())

# Only these five carry a REQUIRED paper locator (`hyperparameters`/`architecture`/
# `preprocessing` are optional — see `harness.reimplement.INGREDIENTS`) and only these
# five are what decision 1 requires bound before a reconstruction may run.
REQUIRED_KINDS = ("method", "training", "dataset", "metric", "comparison_target")

_LINE_SPAN = re.compile(r"\blines?\s+(\d+)(?:\s*[-:–]\s*(\d+))?", re.I)
_NAMED = re.compile(r"\b([A-Za-z_]\w*)\s*(?:\(\)|\((?:function|def)\))|\b(?:function|def)\s+([A-Za-z_]\w*)")


def _implementation_locator_matches(script: str, impl_ref: str, impl_quote: str) -> bool:
    """Verify the quote at the claimed implementation locator, not anywhere in the file.
    A locator is read for every line span ("lines 43-59") and function name ("f()",
    "function f", "f (function)") it names, however phrased; the quote must lie inside
    one of those named spans."""
    if not impl_ref or not impl_quote:
        return False
    lines = script.splitlines()
    ref = impl_ref.strip()
    spans: list[tuple[int, int]] = []
    for m in _LINE_SPAN.finditer(ref):
        start, end = int(m.group(1)), int(m.group(2) or m.group(1))
        if 1 <= start <= end <= len(lines):
            spans.append((start, end))
    names = {a or b for a, b in _NAMED.findall(ref)} | (
        {ref} if re.fullmatch(r"[A-Za-z_]\w*", ref) else set())
    words = set(re.findall(r"[A-Za-z_]\w*", ref))
    try:
        import ast
        tree = ast.parse(script)
        for n in ast.walk(tree):
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in names:
                spans.append((n.lineno, int(getattr(n, "end_lineno", n.lineno))))
        # A module-level constant named in the locator ("DATASET_PATH constant") is a
        # location too: its own assignment line(s), never the rest of the file.
        for n in tree.body:
            targets = (n.targets if isinstance(n, ast.Assign) else
                       [n.target] if isinstance(n, ast.AnnAssign) else [])
            if any(isinstance(t, ast.Name) and t.id in words for t in targets):
                spans.append((n.lineno, int(getattr(n, "end_lineno", n.lineno))))
    except SyntaxError:
        pass
    if any(impl_quote in "\n".join(lines[s - 1:e]) for s, e in spans):
        return True
    # A quote that skips the span's comment/blank lines still quotes its CODE verbatim.
    code = lambda text: "\n".join(x for x in text.splitlines()      # noqa: E731
                                  if x.strip() and not x.strip().startswith("#"))
    q = code(impl_quote)
    return bool(q) and any(q in code("\n".join(lines[s - 1:e])) for s, e in spans)


class ReimplementationDriverError(RuntimeError):
    pass


def role_model(cfg: Config) -> str:
    return delegation.resolve_model(cfg.reimplementation_model, RP.ROLE_SPEC.get("model", ""))


def available(cfg: Config) -> tuple[bool, str]:
    """Is a reconstruction generation task even in scope for this paper? The only gate
    left is `allow_reimplementation_driver` — the delegation CHANNEL (a session subagent)
    is always available from inside a controlling session, so there is no command or
    executable left to resolve."""
    if not cfg.allow_reimplementation_driver:
        return False, "reimplementation-driver gate is closed; set SH_ALLOW_REIMPLEMENTATION_DRIVER=1"
    return True, ""


def _briefs_dir(cfg: Config, pid: str) -> Path:
    return state.project_dir(cfg, pid) / "tasks"


def persist_brief(cfg: Config, pid: str, target_id: str, brief: str) -> Path:
    """Write the generator's brief to a deterministic path `harness.tasks.pending` can
    discover, in place of spawning a CLI process to consume it immediately.

    `routes.attempt_reimplementation_fallback` calls this once per target whenever no
    accepted reconstruction is sealed yet and the driver gate is open — the SAME moment
    it used to call this module's own `run()`. Idempotent: the brief is a pure function of
    the paper and the target, so re-rendering it on every pipeline pass is a cheap
    overwrite, not a new request.
    """
    path = _briefs_dir(cfg, pid) / f"reimpl_gen__{target_id}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(brief, encoding="utf-8")
    return path


def _brief_stamp(cfg: Config, pid: str, target_id: str) -> Path:
    return _briefs_dir(cfg, pid) / "generated" / f"{target_id}.brief_sha256"


def stamp_brief(cfg: Config, pid: str, target_id: str) -> None:
    """Record which generator brief a generation answered (called when it is sealed)."""
    brief = _briefs_dir(cfg, pid) / f"reimpl_gen__{target_id}.md"
    text = brief.read_text(encoding="utf-8") if brief.is_file() else ""
    _brief_stamp(cfg, pid, target_id).write_text(
        hashlib.sha256(text.encode("utf-8")).hexdigest(), encoding="utf-8")


def answered_brief(cfg: Config, pid: str, target_id: str, brief: str) -> bool:
    """Did the sealed generation answer exactly this brief? A verifier's rejection stands
    only for the brief it judged; when the brief has since changed it is asked again."""
    stamp = _brief_stamp(cfg, pid, target_id)
    return stamp.is_file() and stamp.read_text(encoding="utf-8") == hashlib.sha256(
        brief.encode("utf-8")).hexdigest()


def persist_verification_brief(cfg: Config, pid: str, target_id: str,
                               readiness: ReimplementationReadiness, generated_raw: str) -> Path:
    """Write the INDEPENDENT verifier's brief once a generator's answer exists. Built from
    `_verification_brief` against the generator's own proposed script/bindings/replication,
    so the verifier is shown exactly what was proposed and nothing this harness invented."""
    script, bindings, _notes, meta = parse_reimplementation_report(generated_raw)
    brief = _verification_brief(readiness, script, bindings, meta.get("replication"),
                                released=load_released(cfg, pid, target_id))
    path = _briefs_dir(cfg, pid) / f"reimpl_verify__{target_id}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(brief, encoding="utf-8")
    return path


def _verification_brief(readiness: ReimplementationReadiness, script: str,
                        bindings: list[dict], replication: dict | None = None,
                        released: list[dict] | None = None) -> str:
    rel = released_table(list(released or []))
    released_block = (
        "\n=== THE AUTHORS' RELEASED FILES (the script's working directory is their pinned "
        "checkout) ===\n" + rel + "\nIf the script recomputes the claim from these files, "
        "check it opens the right file(s) and computes the paper's stated quantity from them "
        "without altering, filtering beyond the paper's own stated procedure, or regenerating "
        "the data. Only in that case may \"training\" be absent from approved_kinds: nothing "
        "is trained.\n") if rel else ""
    return f"""{RP.SECURITY}

You are the independent verifier, not the generator. Check the proposed reconstruction
against every paper-owned required ingredient below. Reject any invented method,
training, dataset, metric, or comparison detail, and reject a locator whose quoted text
is not present at that exact implementation location. Also reject if the paper states
how many seeds/trials/runs this experiment used and the proposed replication below is
absent, smaller, or quotes a sentence that does not state it.

=== PAPER INGREDIENTS ===
{ingredients_table(readiness)}

{released_block}
=== PROPOSED SCRIPT ===
```python
{script}
```

=== PROPOSED BINDINGS ===
{json.dumps(bindings, ensure_ascii=False)}

=== PROPOSED REPLICATION (runs = seeds the harness will execute) ===
{json.dumps(replication or {"runs": None, "paper_quote": ""}, ensure_ascii=False)}

Give a categorical verdict: APPROVE only if every required ingredient is realized as the
paper states it; REVISE when the attempt is fixable from the paper's own words (name each
required change, citing the paper); UNCHECKABLE only when the paper itself does not supply
what this recomputation needs.

Print only JSON:
{{"approved": true_or_false,
  "verdict": "APPROVE | REVISE | UNCHECKABLE",
  "required_changes": "each concrete change, citing the paper's words ('' if APPROVE)",
  "approved_kinds": ["method","training","dataset","metric","comparison_target"],
  "notes": "short reason"}}
"""


# RELEASED DATA — the authors' own files in the pinned checkout. A reconstruction that
# recomputes a printed quantity FROM them is a RELEASED_DATA_RECOMPUTATION: its dataset is
# the authors' own, not an invention, and nothing is trained, so "training" is exempt —
# but only when the harness itself re-finds a released path inside the dataset binding.
_DATA_SUFFIXES = (".csv", ".tsv", ".json", ".jsonl", ".parquet", ".npy", ".npz", ".pkl",
                  ".h5", ".hdf5", ".xlsx", ".feather", ".arrow")
# ponytail: 60 files / 2 GB per file hashed; a release beyond that lists only the first 60
# data files (sorted) — raise both if a real paper ships more and needs a later one.
_MAX_RELEASED, _MAX_HASH_BYTES = 60, 2 << 30


def released_files(checkout: Path) -> list[dict]:
    """[{path, bytes, sha256, first_line}] for the data files of a checkout, paths relative
    to it with forward slashes. Deterministic, read-only, never raises."""
    out: list[dict] = []
    try:
        root = Path(checkout)
        files = sorted(f for f in root.rglob("*")
                       if f.is_file() and f.suffix.lower() in _DATA_SUFFIXES
                       and ".git" not in f.relative_to(root).parts)
    except OSError:
        return out
    for f in files[:_MAX_RELEASED]:
        try:
            size = f.stat().st_size
            if size > _MAX_HASH_BYTES:
                continue
            h = hashlib.sha256()
            with f.open("rb") as fh:
                for chunk in iter(lambda: fh.read(1 << 20), b""):
                    h.update(chunk)
            first = ""
            if f.suffix.lower() in (".csv", ".tsv", ".jsonl", ".json"):
                with f.open("r", encoding="utf-8", errors="replace") as fh:
                    # The whole header plus a few rows: a truncated header or unseen value
                    # encoding makes the implementer guess which column holds the label and
                    # how it is coded.
                    first = "\n".join(fh.readline().rstrip("\r\n")[:4000]
                                      for _ in range(4)).strip()
            out.append({"path": f.relative_to(root).as_posix(), "bytes": size,
                        "sha256": h.hexdigest(), "first_line": first})
        except OSError:
            continue
    return out


def released_manifest_path(cfg: Config, pid: str, target_id: str) -> Path:
    return _paths(cfg, pid, target_id)[0].with_suffix(".inputs.json")


def load_released(cfg: Config, pid: str, target_id: str) -> list[dict]:
    path = released_manifest_path(cfg, pid, target_id)
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else []
        return [d for d in data if isinstance(d, dict) and d.get("path") and d.get("sha256")]
    except (OSError, ValueError):
        return []


def released_used(script: str, bindings: list[dict], released: list[dict]) -> list[str]:
    """"path@sha256:<hex>" for every released file the DATASET binding opens — the path must
    occur both in the script and in the dataset binding's own quote (whose presence in the
    script `conformance` verifies separately)."""
    ds = next((b for b in bindings if b.get("kind") == "dataset"), None) or {}
    quote = str(ds.get("impl_quote") or "")
    return [f"{r['path']}@sha256:{r['sha256']}" for r in released
            if r["path"] in script and r["path"] in quote]


def required_kinds(script: str, bindings: list[dict], released: list[dict]) -> tuple[str, ...]:
    if released_used(script, bindings, released):
        return tuple(k for k in REQUIRED_KINDS if k != "training")
    return REQUIRED_KINDS


def released_table(released: list[dict]) -> str:
    if not released:
        return ""
    rows = ["| path (relative to the working directory) | bytes | sha256 | header + first rows |",
            "|---|---|---|---|"]
    for r in released:
        first = (r.get("first_line") or "").replace("|", "/").replace("\n", " <br> ")
        rows.append(f"| `{r['path']}` | {r['bytes']} | {r['sha256'][:16]} | {first} |")
    return "\n".join(rows)


# THE VERIFIER'S CATEGORICAL VERDICT — shared with `harness.certificate`. APPROVE is the
# caller's own parser's decision (never the verifier's word alone); an unapproved answer is
# REVISE — its critique earns the generator another bounded round — unless the verifier
# says the claim cannot be checked this way at all (UNCHECKABLE, terminal).
VERDICTS = ("APPROVE", "REVISE", "UNCHECKABLE")


def parse_verdict(raw: str, approved: bool) -> tuple[str, str]:
    """(verdict, required_changes). Old answers without a `verdict` read as REVISE."""
    if approved:
        return "APPROVE", ""
    text = (raw or "").strip()
    try:
        data = json.loads(text[text.find("{"):text.rfind("}") + 1])
    except (ValueError, TypeError):
        data = {}
    data = data if isinstance(data, dict) else {}
    required = " ".join(str(data.get("required_changes") or "").split())[:2000]
    verdict = str(data.get("verdict") or "").strip().upper()
    return ("UNCHECKABLE" if verdict == "UNCHECKABLE" else "REVISE"), required


def read_revision(path: Path, base_sha: str) -> dict:
    """The revision state a NEXT brief is built from, or {} — only while it belongs to the
    same base brief: an upstream change to the brief starts the rounds afresh."""
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    except (OSError, ValueError):
        data = {}
    return data if isinstance(data, dict) and data.get("base_sha256") == base_sha else {}


def record_attempt(path: Path, *, base_sha: str, verdict: str, script: str, required: str,
                   notes: str, max_revisions: int) -> int:
    """This sealed attempt's number. A REVISE with budget left persists what the next
    brief must fix; anything else leaves the file untouched, so the brief stays the one
    just answered and nothing is asked again — the loop ends by construction."""
    attempt = int(read_revision(path, base_sha).get("attempt") or 0) + 1
    if verdict == "REVISE" and attempt < 1 + max(0, int(max_revisions)):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "base_sha256": base_sha, "attempt": attempt, "script": script,
            "required_changes": required, "verifier_notes": " ".join((notes or "").split())[:2000],
        }, ensure_ascii=False), encoding="utf-8")
    return attempt


def revision_path(cfg: Config, pid: str, target_id: str) -> Path:
    return _paths(cfg, pid, target_id)[0].with_suffix(".revision.json")


def base_sha_path(cfg: Config, pid: str, target_id: str) -> Path:
    return _paths(cfg, pid, target_id)[0].with_suffix(".base_sha256")


def sealed_record(cfg: Config, pid: str, target_id: str) -> dict:
    """The sealed sidecar ({} when none): verdict, required changes, attempt, notes."""
    _out, sidecar = _paths(cfg, pid, target_id)
    try:
        return state.read_json(sidecar) if sidecar.is_file() else {}
    except (OSError, ValueError):
        return {}


def _parse_verification(text: str, required: tuple[str, ...] = REQUIRED_KINDS) -> tuple[bool, str]:
    start, end = (text or "").find("{"), (text or "").rfind("}")
    if start < 0 or end <= start:
        return False, "verifier returned no JSON object"
    try:
        data = json.loads(text[start:end + 1])
    except json.JSONDecodeError as e:
        return False, f"verifier JSON invalid: {e}"
    kinds = {str(x) for x in (data.get("approved_kinds") or [])}
    approved = bool(data.get("approved")) and set(required) <= kinds
    return approved, str(data.get("notes") or "")


def ingredients_table(readiness: ReimplementationReadiness) -> str:
    """The paper's own words for EVERY ingredient it supplies, required and optional alike,
    as a markdown table — the generator can need an optional one (architecture,
    preprocessing, hyperparameters) exactly as much as a required one to avoid inventing a
    detail the paper actually states elsewhere. Same shape
    `routes.write_reimplementation_prompt` builds for the manual brief, factored out so
    both channels show the delegate identical evidence."""
    rows = ["| ingredient | required | found at | the paper's own words |", "|---|---|---|---|"]
    for i in readiness.ingredients:
        quote = (i.quote or "").replace("|", "\\|")[:200]
        mark = f"`{i.ref}`" if i.ref else "**MISSING**"
        rows.append(f"| {i.kind} | {'yes' if i.required else 'no'} | {mark} | {quote} |")
    return "\n".join(rows)


def build_brief(readiness: ReimplementationReadiness, *, paper_title: str = "", claim: str = "",
                table_ref: str = "", claimed_cell_value: str = "", paper_text: str = "",
                released: list[dict] | None = None) -> str:
    return RP.build(paper_title, claim, table_ref, claimed_cell_value,
                    ingredients_table(readiness), paper_text,
                    released_table=released_table(list(released or []))).replace("\x00", "")


def parse_reimplementation_report(text: str) -> tuple[str, list[dict], str, dict]:
    """(script, bindings, notes, meta) — the raw JSON, unwrapped and validated.

    Unknown keys are dropped, exactly as `grade_driver`/`verdict_driver` do: a field this
    schema does not name is not carried into anything sealed."""
    outer = (text or "").strip()
    raw, envelope = unwrap_envelope(outer)
    meta: dict = {"envelope": envelope_provenance(envelope) if envelope else {}}
    if envelope and envelope.get("is_error"):
        raise ReimplementationDriverError(f"the reviewer reported an error: {raw[:200]}")
    start, end = raw.find("{"), raw.rfind("}")
    if not raw or start < 0 or end <= start:
        raise ReimplementationDriverError(f"no JSON object in the output (first 120 chars: {raw[:120]!r})")
    try:
        data = json.loads(raw[start:end + 1])
    except json.JSONDecodeError as e:
        raise ReimplementationDriverError(f"output is not valid JSON: {e}") from e
    if not isinstance(data, dict) or not str(data.get("script") or "").strip():
        raise ReimplementationDriverError("output JSON has no non-empty 'script'")
    bindings = data.get("bindings")
    if not isinstance(bindings, list):
        bindings = []
    clean_bindings = [b for b in bindings if isinstance(b, dict) and b.get("kind")]
    rep = data.get("replication") if isinstance(data.get("replication"), dict) else {}
    runs = rep.get("runs")
    if isinstance(runs, int) and not isinstance(runs, bool) and 0 < runs <= 1000:
        meta["replication"] = {"runs": runs, "paper_quote": str(rep.get("paper_quote") or "")}
    return str(data["script"]), clean_bindings, str(data.get("notes") or ""), meta


def conformance(readiness: ReimplementationReadiness, script: str,
                bindings: list[dict], *, generated_by: str = "",
                verified_by: str = "",
                released: list[dict] | None = None) -> ReimplementationConformance:
    """Pure. Pairs each REQUIRED ingredient's PAPER locator (from `readiness`, already
    established) with the driver's IMPLEMENTATION locator, and — the harness's own check,
    never the delegate's — verifies `impl_quote` occurs verbatim in `script`.

    `readiness.established` must already be True before this is called (eligibility is a
    precondition, not something this function re-derives); a caller that skips that check
    is asking whether an implementation stayed inside a brief that was never checked
    complete, which is a different and weaker question.
    """
    by_kind = {i.kind: i for i in readiness.ingredients}
    driver_by_kind = {b.get("kind"): b for b in bindings}
    out: list[ReimplementationBinding] = []
    unbound: list[str] = []
    released = list(released or [])
    used = released_used(script, bindings, released)
    for kind in required_kinds(script, bindings, released):
        paper = by_kind.get(kind)
        paper_ref = paper.ref if paper else ""
        paper_quote = paper.quote if paper else ""
        d = driver_by_kind.get(kind) or {}
        impl_ref = str(d.get("impl_ref") or "").strip()
        impl_quote = str(d.get("impl_quote") or "").strip()
        verified = _implementation_locator_matches(script, impl_ref, impl_quote)
        bound = bool(paper_ref) and bool(impl_ref) and verified
        if not bound:
            unbound.append(kind)
        out.append(ReimplementationBinding(
            kind=kind, paper_ref=paper_ref, paper_quote=paper_quote,
            impl_ref=impl_ref, impl_quote=impl_quote, verified=verified, bound=bound))
    generator = (generated_by or "").strip()
    verifier = (verified_by or "").strip()
    independent = bool(generator and verifier and generator != verifier)
    established = not unbound and independent
    if established:
        reason = ("every required ingredient is bound to both a paper locator and a "
                  "verified implementation locator, and a separately attributed verifier "
                  "approved the proposed bindings; this reconstruction may reconcile "
                  "against the cited cell")
    elif not unbound and not independent:
        reason = ("all proposed snippets are present, but generated code cannot certify "
                  "its own scientific conformance. A separately attributed verifier must "
                  "approve the method, training, dataset, metric and comparison bindings")
    else:
        reason = ("not bound: " + ", ".join(unbound) + " — the returned script does not "
                  "verifiably realize " + ("this ingredient" if len(unbound) == 1 else
                                            "these ingredients") + ", so no verdict may be "
                  "drawn from running it")
    if used:
        reason += ("; it recomputes from the authors' released file(s) " + ", ".join(used)
                   + ", so no training ingredient applies")
    return ReimplementationConformance(
        established=established, bindings=out, unbound=unbound, reason=reason,
        generated_by=generator, verified_by=verifier,
        independently_verified=independent, released_inputs=used)


def _replication(conf: ReimplementationConformance, meta: dict) -> None:
    rep = meta.get("replication") or {}
    conf.replication_runs = int(rep.get("runs") or 0)
    conf.replication_quote = str(rep.get("paper_quote") or "")


def _paths(cfg: Config, pid: str, target_id: str) -> tuple[Path, Path]:
    out = (state.project_dir(cfg, pid) / "runs" / pid / "reimplementation" /
           f"{target_id or 'default'}.json")
    return out, out.with_suffix(".driver.json")


def _seal(cfg: Config, pid: str, target_id: str, script: str,
         conf: ReimplementationConformance, record: dict) -> dict:
    """`record` already carries whatever `delegation.provenance_record` produced —
    `accept_reimplementation` builds it explicitly, and `run()`'s own inline record states
    its mode directly, since it IS a `CLI_SUBPROCESS` call (two of them: a generator and a
    separately attributed verifier). See `verdict_driver._seal` for why
    `mode`/`reviewer`/`tool_policy` are re-derived FROM `record` rather than recomputed a
    second, independent time. The payload is a plain wrapper dict, never a pydantic
    `model_dump()` — `sealing.seal` takes `payload: dict` for exactly this reason.
    """
    out, _sidecar = _paths(cfg, pid, target_id)
    return sealing.seal(
        out, {"script": script, "conformance": conf.model_dump()},
        mode=record.get("delegation_mode", "MANUAL"),
        reviewer=record.get("reviewer") or record.get("reader", ""),
        tool_policy=record.get("tool_policy", "unrecorded"),
        extra={**record, "paper_id": pid, "target_id": target_id,
              "established": conf.established},
    )


def accept_reimplementation(cfg: Config, pid: str, target_id: str, raw: str,
                            readiness: ReimplementationReadiness, *, reviewer: str = "",
                            generated_by: str = "",
                            tool_policy: str = "unrecorded", mode: str = "MANUAL",
                            verdict: str = "", required_changes: str = "",
                            verifier_notes: str = "", attempt: int = 1) -> dict:
    """Seal a reconstruction produced OUTSIDE this module's own subprocess — the same
    manual-acceptance channel `stages.audit.accept_lens` / `grade_stage.accept_grade` /
    `verdict_driver.accept_verdict` already give their phases, completing the set for
    this one."""
    script, bindings, _notes, meta = parse_reimplementation_report(raw)
    conf = conformance(readiness, script, bindings, generated_by=generated_by,
                       verified_by=reviewer, released=load_released(cfg, pid, target_id))
    _replication(conf, meta)
    prov = delegation.provenance_record(mode=mode, reviewer=reviewer, tool_policy=tool_policy)
    return _seal(cfg, pid, target_id, script, conf, {
        "written_by": prov["written_by"], "delegation_mode": prov["delegation_mode"],
        "reviewer": prov["reviewer"], "generated_by": generated_by,
        "independent_verification": conf.independently_verified,
        "verdict": verdict, "required_changes": required_changes,
        "verifier_notes": " ".join((verifier_notes or "").split())[:2000], "attempt": attempt,
        "tool_policy": prov["tool_policy"],
        "isolation_claim": prov["isolation_claim"],
        "tool_policy_provable": prov["tool_policy_provable"]})


def load_accepted(cfg: Config, pid: str, target_id: str, *,
                  prompt_sha256: str = "") -> tuple[str, ReimplementationConformance] | None:
    """A sealed (script, conformance) for `pid`/`target_id`, or None. Verifies the seal
    before trusting it, exactly as `verdict_driver.load_accepted` does."""
    out, sidecar = _paths(cfg, pid, target_id)
    ok, _why = sealing.verify_seal(out, accepted_writers=WRITERS)
    if not ok:
        return None
    try:
        rec = state.read_json(sidecar)
        recorded_prompt = str(rec.get("prompt_sha256") or "")
        if prompt_sha256 and recorded_prompt and recorded_prompt != prompt_sha256:
            return None
        data = state.read_json(out)
        conf = ReimplementationConformance(**data.get("conformance") or {})
        if conf.established and not (rec.get("independent_verification")
                                     and conf.independently_verified
                                     and conf.generated_by and conf.verified_by
                                     and conf.generated_by != conf.verified_by):
            return None
        return str(data.get("script") or ""), conf
    except Exception:
        return None


if __name__ == "__main__":       # self-check: python -m harness.reimplement_driver
    import tempfile as _tf

    from .schema import ReimplementationIngredient

    cfg = Config.load()
    closed = Config(allow_reimplementation_driver=False)
    assert available(closed)[0] is False
    assert available(Config(allow_reimplementation_driver=True))[0] is True

    def _readiness(established=True) -> ReimplementationReadiness:
        ings = [
            ReimplementationIngredient(kind="method", required=True, present=True,
                                       ref="s0", quote="we define the objective as a sum"),
            ReimplementationIngredient(kind="training", required=True, present=True,
                                       ref="s0", quote="Adam for 30 epochs at lr=1e-3"),
            ReimplementationIngredient(kind="dataset", required=True, present=True,
                                       ref="s0", quote="the CIFAR-100 dataset"),
            ReimplementationIngredient(kind="metric", required=True, present=True,
                                       ref="s0", quote="report accuracy"),
            ReimplementationIngredient(kind="comparison_target", required=True, present=True,
                                       ref="T0", quote="Table 1"),
        ]
        return ReimplementationReadiness(established=established, ingredients=ings)

    # --- parsing --------------------------------------------------------------------
    good = json.dumps({
        "script": "import argparse\n# method: sum objective\ndef train():\n    pass\n"
                  "print('SH_METRIC arm=a seed=0 value=0.9')",
        "bindings": [
            {"kind": "method", "impl_ref": "line 2", "impl_quote": "# method: sum objective"},
            {"kind": "training", "impl_ref": "line 3", "impl_quote": "def train():"},
            {"kind": "dataset", "impl_ref": "line 1", "impl_quote": "import argparse"},
            {"kind": "metric", "impl_ref": "line 5", "impl_quote": "SH_METRIC arm=a seed=0 value=0.9"},
            {"kind": "comparison_target", "impl_ref": "line 5", "impl_quote": "SH_METRIC arm=a seed=0 value=0.9"},
        ],
        "notes": "",
    })
    script, bindings, notes, _meta = parse_reimplementation_report(good)
    assert "SH_METRIC" in script and len(bindings) == 5 and notes == ""
    for bad in ("", "no json", '{"bindings":[]}', '{"script":""}'):
        try:
            parse_reimplementation_report(bad)
            raise AssertionError(f"should have rejected {bad!r}")
        except ReimplementationDriverError:
            pass

    # --- conformance: THE REGRESSION this module exists to fix -----------------------
    # A binding the delegate CLAIMS but that does not literally appear in the returned
    # script must never be trusted — that is the whole difference between this path and
    # the old unchecked `driver` seal.
    r = _readiness()
    fully_bound = conformance(r, script, bindings,
                              generated_by="generator", verified_by="verifier")
    assert fully_bound.established, fully_bound.unbound
    assert fully_bound.unbound == []
    assert all(b.verified and b.bound for b in fully_bound.bindings)

    lying = json.loads(good)
    lying["bindings"][0]["impl_quote"] = "this text is not anywhere in the script"
    _, lying_bindings, _, _ = parse_reimplementation_report(json.dumps(lying))
    forged = conformance(r, script, lying_bindings,
                         generated_by="generator", verified_by="verifier")
    assert not forged.established
    assert "method" in forged.unbound
    method_binding = next(b for b in forged.bindings if b.kind == "method")
    assert method_binding.impl_ref == "line 2"          # the delegate's claim is RECORDED
    assert not method_binding.verified and not method_binding.bound  # but not TRUSTED

    # a binding entirely absent from the delegate's report is unbound, not a crash
    missing = conformance(r, script, [b for b in bindings if b["kind"] != "dataset"],
                          generated_by="generator", verified_by="verifier")
    assert "dataset" in missing.unbound and not missing.established

    # a paper ingredient that was never established (readiness refused it) can never
    # become bound no matter what the delegate claims about it
    partial_readiness = ReimplementationReadiness(established=False, ingredients=[
        ReimplementationIngredient(kind="method", required=True, present=False, ref="", quote="")])
    no_paper_ref = conformance(partial_readiness, script, bindings,
                               generated_by="generator", verified_by="verifier")
    method_b = next(b for b in no_paper_ref.bindings if b.kind == "method")
    assert method_b.paper_ref == "" and not method_b.bound, (
        "no paper locator means no binding, regardless of the implementation side")

    # --- ingredients_table / build_brief ----------------------------------------------
    table = ingredients_table(r)
    assert "method" in table and "comparison_target" in table and "MISSING" not in table
    incomplete = _readiness(established=False)
    incomplete.ingredients[0].present = False
    incomplete.ingredients[0].ref = ""
    assert "**MISSING**" in ingredients_table(incomplete)
    # optional ingredients (architecture/preprocessing/hyperparameters) are shown too, not
    # just the five required ones — the generator can need one to avoid inventing a detail
    # the paper actually states.
    with_optional = _readiness()
    with_optional.ingredients.append(ReimplementationIngredient(
        kind="architecture", required=False, present=True, ref="s2", quote="a 3-layer MLP"))
    opt_table = ingredients_table(with_optional)
    assert "architecture" in opt_table and "a 3-layer MLP" in opt_table
    assert "| architecture | no | `s2` | a 3-layer MLP |" in opt_table
    brief = build_brief(r, paper_title="T", claim="c", table_ref="T0:r0:c0",
                        claimed_cell_value="91.4", paper_text="body")
    assert "T0:r0:c0" in brief and "91.4" in brief and "INDEPENDENT_REIMPLEMENTATION" in brief

    # --- seal / load roundtrip, and attribution ---------------------------------------
    with _tf.TemporaryDirectory() as _td:
        _cfg = Config(projects_dir=Path(_td), allow_reimplementation_driver=True)
        assert load_accepted(_cfg, "p", "t1") is None
        _rec = accept_reimplementation(
            _cfg, "p", "t1", good, r, reviewer="alice", generated_by="builder")
        assert _rec["written_by"] in WRITERS and _rec["established"] is True
        got = load_accepted(_cfg, "p", "t1")
        assert got is not None
        got_script, got_conf = got
        assert got_script == script and got_conf.established
        # a different target_id is a different seal
        assert load_accepted(_cfg, "p", "t2") is None
        # a nonconformant submission seals HONESTLY as not established, never silently
        # promoted
        lying_rec = accept_reimplementation(
            _cfg, "p", "t3", json.dumps(lying), r,
            reviewer="alice", generated_by="builder")
        assert lying_rec["established"] is False
        lying_loaded = load_accepted(_cfg, "p", "t3")
        assert lying_loaded is not None
        assert not lying_loaded[1].established
        # an edited artifact is not the artifact that was sealed
        out_path, _ = _paths(_cfg, "p", "t1")
        state.write_json(out_path, {"script": "tampered", "conformance": {}})
        assert load_accepted(_cfg, "p", "t1") is None

    # --- model tier metadata ------------------------------------------------------------
    assert role_model(Config(reimplementation_model="haiku")) == "haiku"
    assert role_model(Config(reimplementation_model="")) == RP.ROLE_SPEC["model"]

    # --- persist_brief / persist_verification_brief: the session-subagent task protocol,
    # in place of a CLI subprocess this module used to spawn twice per target -----------
    with _tf.TemporaryDirectory() as _td2:
        _cfg2 = Config(projects_dir=Path(_td2), allow_reimplementation_driver=True)
        state.create_project(_cfg2, "", "T", pid="p")
        gen_path = persist_brief(_cfg2, "p", "t1", "GENERATE THIS")
        assert gen_path.is_file() and gen_path.read_text(encoding="utf-8") == "GENERATE THIS"
        assert gen_path == _briefs_dir(_cfg2, "p") / "reimpl_gen__t1.md"
        verify_path = persist_verification_brief(_cfg2, "p", "t1", r, good)
        assert verify_path.is_file()
        verify_text = verify_path.read_text(encoding="utf-8")
        assert "independent verifier" in verify_text and "SH_METRIC" in verify_text

    # --- this module WRITES and never RUNS what it writes; never touches the modules
    # that decide whether a sealed reconstruction may execute, and never spawns a process -
    import ast as _ast
    import inspect as _inspect
    import sys as _sys

    _tree = _ast.parse(_inspect.getsource(_sys.modules[__name__]))
    _imported: set[str] = set()
    for _node in _ast.walk(_tree):
        if isinstance(_node, _ast.ImportFrom):
            _imported.add(_node.module or "")
            _imported.update(a.name for a in _node.names)
        elif isinstance(_node, _ast.Import):
            _imported.update(a.name.split(".")[0] for a in _node.names)
    for _forbidden in ("backends", "local_exec", "stages", "planner", "grading", "taxonomy",
                      "subprocess", "os"):
        assert _forbidden not in _imported, _forbidden

    print(json.dumps({"self_check": "ok",
                      "gate_default": cfg.allow_reimplementation_driver}, indent=2))
