"""Seal a lens reading produced by an isolated subagent of the controlling session.

`run.py accept` seals a WHOLE-paper lens file and nothing else. A paper that does not
fit one pass is read as N bounded parts plus one synthesis per lens, and those units are
sealed by `audit_driver.run_lens` — which spawns a CLI subprocess. So the only way to
read a long paper without that subprocess was to side-write the part file, which
`unit_is_accepted` correctly refuses: no sidecar, no provenance, no prompt pinning.

This is that gate for a part and a synthesis, through the SAME validation
(`audit_driver.parse_lens_json`) and the SAME seal (`content_sha256` over the written
bytes, `prompt_sha256` over the prompt that produced it). What it may claim is decided
by `harness.delegation`, not here: SESSION_SUBAGENT reports real context isolation and
`tool_policy: unrecorded`, because this session did not build the delegate's argv and
cannot prove what it could reach.

    python tools/subagent_accept.py pending <paper-id> [...]
    python tools/subagent_accept.py seal <paper-id> <unit-id> <staged-file>
    python tools/subagent_accept.py reimpl-brief <paper-id> <out-dir>
    python tools/subagent_accept.py reimpl-verify-brief <paper-id> <generated.json> <out.md>
    python tools/subagent_accept.py reimpl-seal <paper-id> <target-id> <generated.json> [<verdict.json>]

`unit-id` is what `AuditUnit.unit_id` prints: `overclaim` for a whole-paper reading,
`overclaim/part-02` or `overclaim/synthesis` for a split one.
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness import agent, reimplement_driver, state  # noqa: E402
from harness import audit as audit_stage  # noqa: E402
from harness.config import Config  # noqa: E402
from harness.schema import PaperDoc  # noqa: E402

REVIEWER = "desktop session subagent (Agent tool), one isolated context per reading"


def units(cfg: Config, pid: str) -> list[audit_stage.AuditUnit]:
    root = state.project_dir(cfg, pid)
    doc = PaperDoc(**state.read_json(root / "paper" / "doc.json"))
    return audit_stage.units_for(root, audit_stage.LENSES, audit_stage.plan_for(doc))


def pending(cfg: Config, pid: str) -> list[dict]:
    """Every reading this paper still needs, with the prompt that asks for it.

    A synthesis whose parts are not all sealed is reported as BLOCKED rather than
    pending: `run_audit` refuses to render its prompt until they are, so dispatching it
    would be asking a question whose input does not exist yet.
    """
    all_units = units(cfg, pid)
    sealed = {u.unit_id for u in all_units if audit_stage.unit_is_accepted(u)[0]}
    out = []
    for u in all_units:
        if u.unit_id in sealed:
            continue
        parts = [p for p in all_units if p.lens == u.lens and p.kind == "part"]
        blocked = (u.kind == "synthesis"
                   and not all(p.unit_id in sealed for p in parts))
        out.append({"unit_id": u.unit_id, "lens": u.lens, "kind": u.kind,
                    "prompt": str(u.prompt_path),
                    "prompt_exists": u.prompt_path.is_file(),
                    "out": str(u.out_path),
                    "state": "BLOCKED_ON_PARTS" if blocked else
                             ("PENDING" if u.prompt_path.is_file() else "NO_PROMPT_YET")})
    return out


def seal(cfg: Config, pid: str, unit_id: str, staged: Path) -> dict:
    """Validate a staged reading and write it plus its provenance sidecar."""
    match = [u for u in units(cfg, pid) if u.unit_id == unit_id]
    if not match:
        raise SystemExit(f"{pid}: no such unit {unit_id!r}; run `pending` to list them")
    unit = match[0]
    raw = staged.read_text(encoding="utf-8")
    report = agent.parse_lens_json(raw, unit.lens)   # the one validation path
    unit.out_path.parent.mkdir(parents=True, exist_ok=True)
    state.write_json(unit.out_path, report.model_dump())
    # The raw response is kept beside the accepted one for the same reason `run_lens`
    # keeps its own: a reader tracing a finding must be able to see what was returned
    # before this harness touched it.
    raw_path = unit.out_path.with_suffix(".raw.txt")
    raw_path.write_text(raw, encoding="utf-8")
    record = {
        "lens": unit.lens, "unit_id": unit.unit_id, "paper_id": pid,
        **agent.provenance_record(mode="SESSION_SUBAGENT", reviewer=REVIEWER),
        "controller": "claude code desktop (Claude Opus 5 session)",
        "model_requested": "sonnet",
        "authorization": "RUN_AUTHORIZATION.json",
        "content_sha256": hashlib.sha256(unit.out_path.read_bytes()).hexdigest(),
        "prompt_sha256": agent.prompt_fingerprint(unit.prompt_path),
        "prompt_path": str(unit.prompt_path),
        "raw_sha256": hashlib.sha256(raw_path.read_bytes()).hexdigest(),
        "raw_response": raw_path.name,
        "findings": len(report.findings),
        "ts": state.now(),
    }
    state.write_json(unit.sidecar_path, record)
    ok, why = audit_stage.unit_is_accepted(unit)
    if not ok:
        raise SystemExit(f"{pid}/{unit_id}: sealed and still refused: {why}")
    return record


STAGE_DIR = ".staged_subagent"


def sweep(cfg: Config, pid: str) -> list[dict]:
    """Seal every staged reading for one paper, and consume what it sealed.

    A staged file that survives sealing gets re-sealed on the next sweep, rewriting a
    sealed artifact's sidecar with a fresh timestamp and making a re-run look like new
    work — the same reason `audit_driver.run_lens` unlinks its own staging file after
    promotion. The promoted copy is the record.

    The filename carries the unit: `<lens>.json` is a whole-paper reading and
    `<lens>__part-02.json` / `<lens>__synthesis.json` are the split ones.
    """
    root = state.project_dir(cfg, pid) / "audit" / STAGE_DIR
    out = []
    for src in sorted(root.glob("*.json")) if root.is_dir() else []:
        unit_id = src.stem.replace("__", "/")
        try:
            rec = seal(cfg, pid, unit_id, src)
        except SystemExit as e:
            out.append({"unit_id": unit_id, "error": str(e)})
            continue
        except Exception as e:                        # noqa: BLE001 — kept, never deleted
            src.replace(src.with_suffix(".rejected.txt"))
            out.append({"unit_id": unit_id, "error": f"{type(e).__name__}: {e}"})
            continue
        src.unlink(missing_ok=True)
        out.append(rec)
    return out


def main(argv: list[str]) -> int:
    if not argv:
        raise SystemExit(__doc__)
    cfg = Config.load()
    if argv[0] == "sweep":
        bad = 0
        for pid in argv[1:]:
            for rec in sweep(cfg, pid):
                if "error" in rec:
                    bad += 1
                    print(f"REFUSED  {pid}/{rec['unit_id']}: {rec['error'][:160]}")
                else:
                    print(f"sealed   {pid}/{rec['unit_id']}: {rec['findings']} finding(s)")
        return 1 if bad else 0
    if argv[0] == "pending":
        rows = {pid: pending(cfg, pid) for pid in argv[1:]}
        print(json.dumps(rows, indent=1))
        return 0
    if argv[0] == "seal":
        rec = seal(cfg, argv[1], argv[2], Path(argv[3]))
        print(f"{argv[1]}/{argv[2]}: {rec['findings']} finding(s) "
              f"sha={rec['content_sha256'][:12]} mode={rec['delegation_mode']}")
        return 0
    if argv[0] == "reimpl-brief":
        for tid, path in reimpl_briefs(cfg, argv[1], Path(argv[2])):
            print(f"brief    {argv[1]}/{tid}: {path}")
        return 0
    if argv[0] == "reimpl-verify-brief":
        ready = _readiness(cfg, argv[1])
        script, bindings, _notes, meta = reimplement_driver.parse_reimplementation_report(
            Path(argv[2]).read_text(encoding="utf-8"))
        Path(argv[3]).write_text(reimplement_driver._verification_brief(
            ready, script, bindings, meta.get("replication")), encoding="utf-8")
        print(f"verify   {argv[3]}")
        return 0
    if argv[0] == "reimpl-seal":
        conf = reimpl_seal(cfg, argv[1], argv[2], Path(argv[3]),
                           Path(argv[4]) if len(argv) > 4 else None)
        print(f"{argv[1]}/{argv[2]}: established={conf.established} — {conf.reason[:160]}")
        return 0
    raise SystemExit(__doc__)


# --- governed reconstruction through session subagents ---------------------------------
# `reimplement_driver.run` is the only automated writer, and it spawns CLI subprocesses.
# These three commands are the same brief, the same independent-verifier brief and the
# same `accept_reimplementation` seal, with the generator and the verifier being two
# separate session subagents. The verifier is named on the seal ONLY if its reply parses
# as approved under `reimplement_driver._parse_verification` — the rule `run()` applies —
# so a rejected or absent verdict seals a reconstruction `authorize()` will refuse.

def _readiness(cfg: Config, pid: str):
    from harness import discover
    doc = PaperDoc(**state.read_json(state.project_dir(cfg, pid) / "paper" / "doc.json"))
    return discover.reimplementation_readiness(doc)


def reimpl_briefs(cfg: Config, pid: str, out_dir: Path) -> list[tuple[str, Path]]:
    """One generator brief per INDEPENDENT_RECONSTRUCTION target, built exactly as
    `routes.attempt_reimplementation_fallback` builds it."""
    from harness import routes
    doc = PaperDoc(**state.read_json(state.project_dir(cfg, pid) / "paper" / "doc.json"))
    ready = _readiness(cfg, pid)
    if not ready.established:
        raise SystemExit(f"{pid}: the paper does not specify enough for a reconstruction")
    _ts, pairs, deferred = routes._executable_targets(cfg, pid)
    out_dir.mkdir(parents=True, exist_ok=True)
    out = []
    for obj, plan in pairs + deferred:
        if plan.route != "INDEPENDENT_RECONSTRUCTION":
            continue
        spec = routes.build_spec(cfg, pid, doc, obj)
        tid = spec.target_id or "default"
        path = out_dir / f"{tid}.brief.md"
        path.write_text(reimplement_driver.build_brief(
            ready, paper_title=doc.title, claim=spec.claim, table_ref=spec.table_ref,
            claimed_cell_value=spec.claimed_cell_value,
            paper_text=routes._full_paper_text(doc)), encoding="utf-8")
        out.append((tid, path))
    return out


def reimpl_seal(cfg: Config, pid: str, target_id: str, generated: Path,
                verdict: Path | None):
    approved = False
    if verdict is not None and verdict.is_file():
        approved, _why = reimplement_driver._parse_verification(
            verdict.read_text(encoding="utf-8"))
    reimplement_driver.accept_reimplementation(
        cfg, pid, target_id, generated.read_text(encoding="utf-8"), _readiness(cfg, pid),
        reviewer="session subagent verifier (separate isolated context)" if approved else "",
        generated_by="session subagent generator (isolated context)",
        mode="SESSION_SUBAGENT")
    sealed = reimplement_driver.load_accepted(cfg, pid, target_id)
    if sealed is None:
        raise SystemExit(f"{pid}/{target_id}: sealed and still refused by load_accepted")
    return sealed[1]


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
